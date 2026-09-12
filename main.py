"""
Вакансии из официального API «Работа в России» (opendata.trudvsem.ru)
по списку компаний -> теги -> пост в Telegram.

Запуск: python main.py
Требуемые переменные окружения:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    GEMINI_API_KEY   (нужен для генерации описания вакансии и доп. тегов;
                       без него в посте не будет текста-описания, только
                       "сухие" поля напрямую из API)
"""

import os
import re
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

# Промпт для единого вызова LLM: описание + семантические теги за один запрос,
# чтобы не тратить квоту на два отдельных вызова.
DESCRIPTION_PROMPT = """Ты помогаешь оформлять посты о вакансиях для Telegram-канала.
На основе данных вакансии ниже сделай:
1. "description" — краткое (2-4 предложения) человеческое описание вакансии:
   что предстоит делать и что важно для кандидата. Пиши живо, но по делу,
   без канцелярита и без повторения того, что уже есть в других полях поста
   (зарплата, регион, график и опыт указывать НЕ нужно — это уже есть отдельно).
2. "tags" — список из 0-2 тегов СТРОГО из разрешённого списка: {allowed_tags}.
   Указывай тег, только если он явно следует из текста ниже. Если ничего не
   подходит — пустой список.

Ответь строго JSON-объектом вида {{"description": "...", "tags": ["..."]}},
без пояснений и без markdown-разметки.

Должность: {job_name}
Обязанности: {duty}
Требования: {requirements}
Квалификация/разряд: {qualification}
Льготы/условия: {benefit}
"""


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
    """requirement.experience в реальном API — уже число лет (int)."""
    exp_raw = (requirement or {}).get("experience")
    if exp_raw is None:
        return None
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


def get_address(v: dict) -> str | None:
    """Человекочитаемый адрес из addresses.address[0].location, если есть."""
    addresses = (v.get("addresses") or {}).get("address") or []
    if addresses and isinstance(addresses, list):
        return addresses[0].get("location")
    return None


def guess_remote(v: dict) -> bool:
    """В API нет явного флага 'удалёнка' — определяем по тексту schedule/employment."""
    text = " ".join([
        str(v.get("schedule", "")),
        str(v.get("employment", "")),
        str(v.get("job-name", "")),
    ]).lower()
    return any(kw in text for kw in ["удален", "дистанц", "надомн"])


def sanitize_tag(text: str) -> str:
    """Приводит произвольный текст к валидному Telegram-хэштегу:
    только буквы/цифры/подчёркивания, без скобок и прочих спецсимволов."""
    text = text.replace(" ", "_")
    text = re.sub(r"[^\w]", "", text, flags=re.UNICODE)
    return text.strip("_")


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
        # Берём часть до скобок (например "Татарстан" из "Республика Татарстан (Татарстан)")
        clean_region = region_name.split("(")[0].strip()
        tag = sanitize_tag(clean_region)
        if tag:
            tags.add(tag)

    return tags


def format_benefits(v: dict) -> str | None:
    """API отдаёт льготы через запятую без пробелов — расставляем читаемо."""
    raw = v.get("benefit")
    if not raw:
        return None
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return " · ".join(items)


PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yandex.ru", "ya.ru", "mail.ru", "rambler.ru",
    "bk.ru", "inbox.ru", "list.ru", "outlook.com", "hotmail.com",
    "icloud.com", "yahoo.com",
}


def get_contacts(v: dict) -> dict:
    """
    Достаёт телефон/почту/контактное лицо и пытается угадать официальный сайт
    компании по домену корпоративной почты (не gmail/yandex/mail.ru и т.п.).
    Это эвристика: если почта на общем почтовом сервисе — сайт не угадываем.
    """
    contact_list = v.get("contact_list") or []
    phone, email = None, None
    for c in contact_list:
        ctype = (c.get("contact_type") or "").lower()
        if "телефон" in ctype and not phone:
            phone = c.get("contact_value")
        elif "почта" in ctype and not email:
            email = c.get("contact_value")

    company_website = None
    if email and "@" in email:
        domain = email.split("@")[-1].strip().lower()
        if domain and domain not in PERSONAL_EMAIL_DOMAINS:
            company_website = f"https://{domain}"

    # У компании в API есть ещё явное поле url — но это профиль на trudvsem.ru,
    # а не сайт компании, поэтому его не используем как company_website.

    return {
        "phone": phone,
        "email": email,
        "contact_person": v.get("contact_person"),
        "company_website": company_website,
    }


def format_salary(v: dict) -> str | None:
    lo, hi = v.get("salary_min"), v.get("salary_max")
    lo = lo if lo else None
    hi = hi if hi else None
    if not lo and not hi:
        # Иногда есть только текстовое поле "salary" (например "от 124000")
        salary_text = v.get("salary")
        return f"{salary_text} ₽" if salary_text else None
    if lo and hi and lo != hi:
        return f"{int(lo):,} – {int(hi):,} ₽".replace(",", " ")
    val = lo or hi
    return f"от {int(val):,} ₽".replace(",", " ")


# ---------- Описание + семантические теги через Gemini (один вызов) ----------

_gemini_client = None

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None and GEMINI_API_KEY:
        from google import genai
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def generate_description_and_tags(v: dict) -> tuple[str | None, list[str]]:
    """
    Один вызов к Gemini: возвращает (краткое описание вакансии, доп. теги).
    Если GEMINI_API_KEY не задан или запрос не удался — возвращает (None, []).
    """
    client = get_gemini_client()
    if not client:
        return None, []

    from google.genai import types

    requirement = v.get("requirement") or {}
    prompt = DESCRIPTION_PROMPT.format(
        allowed_tags=", ".join(SEMANTIC_ALLOWED_TAGS),
        job_name=v.get("job-name", ""),
        duty=(v.get("duty") or "")[:1500],
        requirements=(v.get("requirements") or "")[:1000],
        qualification=v.get("qualification") or requirement.get("education") or "",
        benefit=(v.get("benefit") or "")[:500],
    )

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.4,
            ),
        )
        data = json.loads(response.text)
        description = data.get("description")
        tags = [t for t in data.get("tags", []) if t in SEMANTIC_ALLOWED_TAGS]
        return description, tags
    except Exception as e:
        print(f"  [warn] генерация описания/тегов не удалась: {e}")
        return None, []


# ---------- Рендер поста ----------

def render_post(v: dict, template_text: str) -> str:
    requirement = v.get("requirement") or {}
    experience_label = guess_experience_label(requirement)
    remote = guess_remote(v)

    tags = build_base_tags(v, experience_label, remote)

    description, extra_tags = generate_description_and_tags(v)
    tags.update(sanitize_tag(t) for t in extra_tags if sanitize_tag(t))

    employment_key = "full" if "полн" in str(v.get("employment", "")).lower() else (
        "part" if "част" in str(v.get("employment", "")).lower() else "unknown"
    )

    contacts = get_contacts(v)

    template = Template(template_text)
    rendered = template.render(
        title=v.get("job-name", "Без названия"),
        company=v.get("_company_display_name") or (v.get("company") or {}).get("name", ""),
        region=(v.get("region") or {}).get("name", "Не указано"),
        address=get_address(v),
        remote=remote,
        employment_label=EMPLOYMENT_LABELS[employment_key],
        schedule=v.get("schedule"),
        experience_label=experience_label,
        salary_text=format_salary(v),
        description=description,
        benefits=format_benefits(v),
        phone=contacts["phone"],
        email=contacts["email"],
        contact_person=contacts["contact_person"],
        company_website=contacts["company_website"],
        vac_url=v.get("vac_url", ""),
        tags=[f"#{t}" for t in sorted(tags)],
    )
    # Схлопываем 3+ подряд пустых строк, которые могут возникнуть из-за
    # необязательных {% if %}-блоков в шаблоне (например, когда нет description)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


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
