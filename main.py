"""
Вакансии из официального API «Работа в России» (opendata.trudvsem.ru)
по списку компаний -> теги -> пост в Telegram.

Запуск: python main.py
Требуемые переменные окружения:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    GEMINI_API_KEY   (опционально — для доп. семантических тегов;
                       если не задан, работает только на детерминированных тегах)
"""

import os
import json
import time
import yaml
import httpx
from jinja2 import Template

# ---------- Конфигурация ----------

COMPANIES_FILE = "companies.yaml"
TEMPLATE_FILE = "template.md"
STATE_FILE = "state.json"

API_BASE = "https://opendata.trudvsem.ru/api/v1/vacancies/company"
PAGE_LIMIT = 100  # максимум, который отдаёт API за один запрос

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # необязателен

GEMINI_MODEL = "gemini-2.0-flash"

# Белый список для семантических тегов (генерируются LLM из текста вакансии,
# если GEMINI_API_KEY задан). Детерминированные теги (город/опыт/занятость/
# удалёнка) генерируются напрямую из полей API без LLM — см. build_base_tags().
SEMANTIC_ALLOWED_TAGS = [
    "релокация", "вахта", "график5_2", "сменный_график",
    "соцпакет", "жильё", "молодежная_программа",
]

EMPLOYMENT_LABELS = {
    "full": "полная занятость",
    "part": "частичная занятость",
    "unknown": "занятость не указана",
}


# ---------- Состояние (что уже опубликовано) ----------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"posted_ids": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- Сбор вакансий из API ----------

def fetch_company_vacancies(company: dict) -> list[dict]:
    """Постранично забирает все вакансии компании по ИНН/ОГРН."""
    id_type = company["id_type"]
    company_id = str(company["id"])
    url = f"{API_BASE}/{id_type}/{company_id}"

    all_vacancies = []
    offset = 0
    with httpx.Client(timeout=30) as client_http:
        while True:
            params = {"limit": PAGE_LIMIT, "offset": offset}
            try:
                resp = client_http.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  [error] запрос к API упал: {e}")
                break

            vacancies = data.get("results", {}).get("vacancies", [])
            if not vacancies:
                break

            for item in vacancies:
                v = item.get("vacancy", {})
                if v:
                    all_vacancies.append(v)

            if len(vacancies) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT

    return all_vacancies


def collect_all_vacancies(companies: list[dict]) -> list[dict]:
    all_vacancies = []
    for company in companies:
        print(f"[fetch] {company['name']} ({company['id_type']}={company['id']})")
        try:
            items = fetch_company_vacancies(company)
            for item in items:
                item["_company_display_name"] = company["name"]
            all_vacancies.extend(items)
            print(f"  -> найдено {len(items)} вакансий")
        except Exception as e:
            print(f"  [error] компания {company['name']} упала: {e}")
    return all_vacancies


# ---------- Детерминированные теги и поля напрямую из API ----------

def guess_experience_label(requirement: dict) -> str | None:
    """API отдаёт requirement.experience как число лет (может быть строкой)."""
    exp_raw = (requirement or {}).get("experience")
    try:
        years = int(exp_raw)
    except (TypeError, ValueError):
        return None

    if years <= 0:
        return "без опыта"
    if years <= 3:
        return "опыт 1-3 года"
    if years <= 6:
        return "опыт 3-6 лет"
    return "опыт 6+ лет"


def guess_remote(v: dict) -> bool:
    """В API нет явного флага 'удалёнка' — определяем по тексту schedule/employment."""
    text = " ".join([
        str(v.get("schedule", "")),
        str(v.get("employment", "")),
        str(v.get("job-name", "")),
    ]).lower()
    return any(kw in text for kw in ["удален", "дистанц", "надомн"])


def build_base_tags(v: dict, experience_label: str | None, remote: bool) -> set[str]:
    tags = set()

    if remote:
        tags.add("удаленка")

    if experience_label == "без опыта":
        tags.add("безопыта")

    schedule = str(v.get("schedule", "")).lower()
    if "вахт" in schedule:
        tags.add("вахта")
    if "смен" in schedule:
        tags.add("сменный_график")

    employment = str(v.get("employment", "")).lower()
    if "полн" in employment:
        tags.add("фуллтайм")
    elif "част" in employment:
        tags.add("парттайм")

    region_name = (v.get("region") or {}).get("name")
    if region_name:
        # Упрощаем название региона до одного тега без пробелов
        tags.add(region_name.replace(" ", "_").replace(",", ""))

    return tags


def format_salary(v: dict) -> str | None:
    lo, hi = v.get("salary_min"), v.get("salary_max")
    lo = lo if lo else None
    hi = hi if hi else None
    if not lo and not hi:
        return None
    if lo and hi and lo != hi:
        return f"{int(lo):,} – {int(hi):,} ₽".replace(",", " ")
    val = lo or hi
    return f"от {int(val):,} ₽".replace(",", " ")


# ---------- Опциональные семантические теги через Gemini ----------

_gemini_client = None

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None and GEMINI_API_KEY:
        from google import genai
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def guess_semantic_tags(duty: str, requirement_text: str) -> list[str]:
    """Необязательный шаг: просим Gemini выбрать 0-2 доп. тега по смыслу текста."""
    client = get_gemini_client()
    if not client:
        return []

    from google.genai import types

    prompt = (
        "Из следующего описания вакансии выбери 0-2 тега, которые ПО СМЫСЛУ "
        f"подходят, строго из списка: {', '.join(SEMANTIC_ALLOWED_TAGS)}. "
        "Если ничего явно не подходит — верни пустой массив. "
        "Ответь JSON-массивом строк, без пояснений.\n\n"
        f"Обязанности: {duty[:1500]}\n"
        f"Требования: {requirement_text[:1500]}"
    )
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        tags = json.loads(response.text)
        if isinstance(tags, list):
            return [t for t in tags if t in SEMANTIC_ALLOWED_TAGS]
    except Exception as e:
        print(f"  [warn] семантические теги не удались: {e}")
    return []


# ---------- Рендер поста ----------

def render_post(v: dict, template_text: str) -> str:
    requirement = v.get("requirement") or {}
    experience_label = guess_experience_label(requirement)
    remote = guess_remote(v)

    tags = build_base_tags(v, experience_label, remote)

    duty = v.get("duty", "") or ""
    requirement_text = requirement.get("qualification", "") or ""
    tags.update(guess_semantic_tags(duty, requirement_text))

    employment_key = "full" if "полн" in str(v.get("employment", "")).lower() else (
        "part" if "част" in str(v.get("employment", "")).lower() else "unknown"
    )

    template = Template(template_text)
    return template.render(
        title=v.get("job-name", "Без названия"),
        company=v.get("_company_display_name") or (v.get("company") or {}).get("name", ""),
        region=(v.get("region") or {}).get("name", "Не указано"),
        remote=remote,
        employment_label=EMPLOYMENT_LABELS[employment_key],
        experience_label=experience_label,
        salary_text=format_salary(v),
        tags=[f"#{t}" for t in sorted(tags)],
        url=v.get("vac_url", ""),
    )


# ---------- Отправка в Telegram ----------

def send_to_telegram(text: str) -> None:
    resp = httpx.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  [error] Telegram API: {resp.status_code} {resp.text}")
    resp.raise_for_status()


# ---------- Main pipeline ----------

def main():
    with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
        companies = yaml.safe_load(f)

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        template_text = f.read()

    state = load_state()
    posted_ids = set(state.get("posted_ids", []))

    vacancies = collect_all_vacancies(companies)
    print(f"\nВсего собрано вакансий: {len(vacancies)}")

    new_count = 0
    for v in vacancies:
        vac_id = v.get("id")
        if not vac_id or vac_id in posted_ids:
            continue

        try:
            post_text = render_post(v, template_text)
            send_to_telegram(post_text)
            posted_ids.add(vac_id)
            new_count += 1
            print(f"  [posted] {v.get('job-name')} — {v.get('_company_display_name')}")
            time.sleep(3)  # анти-флуд лимит Telegram
        except Exception as e:
            print(f"  [error] не удалось обработать/отправить вакансию {vac_id}: {e}")

    state["posted_ids"] = list(posted_ids)
    save_state(state)
    print(f"\nГотово. Новых постов: {new_count}")


if __name__ == "__main__":
    main()
