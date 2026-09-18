"""
Вакансии из API «Работа в России» -> теги + список обязанностей (без LLM) ->
нативная отложенная публикация в Telegram-канал через MTProto (Telethon).

Скрипт запускается раз в день по крону (GitHub Actions), ставит посты в
отложку канала на текущее время + POST_DELAY_HOURS и завершается. Telegram
сам публикует их по расписанию — работающий процесс для этого не нужен.
"""

import os
import re
import json
import yaml
import httpx
import asyncio
from datetime import datetime, timedelta, timezone
from jinja2 import Template
from telethon import TelegramClient
from telethon.sessions import StringSession

COMPANIES_FILE = "companies.yaml"
TEMPLATE_FILE = "template.md"
SEEN_FILE = "seen_ids.json"

API_BASE = "https://opendata.trudvsem.ru/api/v1/vacancies/company"
PAGE_LIMIT = 100
POST_DELAY_HOURS = 6  # через сколько часов после запуска публиковать

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_STRING = os.environ["TELEGRAM_SESSION"]
CHANNEL = os.environ["TELEGRAM_CHANNEL"]  # @username канала или его numeric id

EMPLOYMENT_LABELS = {
    "full": "полная занятость",
    "part": "частичная занятость",
    "unknown": "занятость не указана",
}


# ---------- seen_ids ----------

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


# ---------- Сбор вакансий из API ----------

def fetch_company_vacancies(company: dict) -> list[dict]:
    id_type = company["id_type"]
    company_id = re.sub(r"\s+", "", str(company["id"]).strip())
    url = f"{API_BASE}/{id_type}/{company_id}"

    all_vacancies = []
    offset = 0
    with httpx.Client(timeout=30) as client_http:
        while True:
            try:
                resp = client_http.get(url, params={"limit": PAGE_LIMIT, "offset": offset})
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


# ---------- Теги и поля напрямую из API (без LLM) ----------

def guess_experience_label(requirement: dict) -> str | None:
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
    addresses = (v.get("addresses") or {}).get("address") or []
    if addresses and isinstance(addresses, list):
        return addresses[0].get("location")
    return None


def guess_remote(v: dict) -> bool:
    text = " ".join([
        str(v.get("schedule", "")),
        str(v.get("employment", "")),
        str(v.get("job-name", "")),
    ]).lower()
    return any(kw in text for kw in ["удален", "дистанц", "надомн"])


def sanitize_tag(text: str) -> str:
    text = text.replace(" ", "_")
    text = re.sub(r"[^\w]", "", text, flags=re.UNICODE)
    return text.strip("_")


def build_base_tags(v: dict, experience_label: str | None, remote: bool) -> set:
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
        clean_region = region_name.split("(")[0].strip()
        tag = sanitize_tag(clean_region)
        if tag:
            tags.add(tag)
    return tags


def format_benefits(v: dict) -> str | None:
    raw = v.get("benefit")
    if not raw:
        return None
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return " · ".join(items)


def split_into_bullets(text: str, max_items: int = 6) -> list[str]:
    """Разбивает 'duty' (обязанности) на пункты без LLM — по явным
    разделителям (переносы строк, буллеты, нумерация), а если их нет —
    по предложениям."""
    if not text:
        return []
    text = text.strip()

    parts = re.split(r"[\n;•]|(?:^|\s)-\s+|(?:^|\s)\d+[.)]\s+", text)
    parts = [p.strip(" .;-") for p in parts if p and p.strip(" .;-")]

    if len(parts) < 2:
        parts = re.split(r"(?<=[.!?])\s+", text)
        parts = [p.strip(" .;-") for p in parts if p and p.strip(" .;-")]

    return parts[:max_items]


PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yandex.ru", "ya.ru", "mail.ru", "rambler.ru",
    "bk.ru", "inbox.ru", "list.ru", "outlook.com", "hotmail.com",
    "icloud.com", "yahoo.com",
}


def get_contacts(v: dict) -> dict:
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
        salary_text = v.get("salary")
        return f"{salary_text} ₽" if salary_text else None
    if lo and hi and lo != hi:
        return f"{int(lo):,} – {int(hi):,} ₽".replace(",", " ")
    val = lo or hi
    return f"от {int(val):,} ₽".replace(",", " ")


# ---------- Рендер поста ----------

def render_post(v: dict, template_text: str) -> str:
    requirement = v.get("requirement") or {}
    experience_label = guess_experience_label(requirement)
    remote = guess_remote(v)
    tags = build_base_tags(v, experience_label, remote)

    employment_key = "full" if "полн" in str(v.get("employment", "")).lower() else (
        "part" if "част" in str(v.get("employment", "")).lower() else "unknown"
    )
    contacts = get_contacts(v)
    duties = split_into_bullets(v.get("duty"))

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
        duties=duties,
        benefits=format_benefits(v),
        phone=contacts["phone"],
        email=contacts["email"],
        contact_person=contacts["contact_person"],
        company_website=contacts["company_website"],
        vac_url=v.get("vac_url", ""),
        tags=[f"#{t}" for t in sorted(tags)],
    )
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


# ---------- Основной сценарий ----------

async def main():
    with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
        companies = yaml.safe_load(f)
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        template_text = f.read()

    seen = load_seen()
    vacancies = collect_all_vacancies(companies)
    print(f"\nВсего собрано вакансий: {len(vacancies)}")

    new_posts = []
    for v in vacancies:
        vac_id = v.get("id")
        if not vac_id or vac_id in seen:
            continue
        try:
            post_text = render_post(v, template_text)
        except Exception as e:
            print(f"  [error] не удалось отрендерить вакансию {vac_id}: {e}")
            continue
        new_posts.append((vac_id, v.get("job-name"), v.get("_company_display_name"), post_text))

    if not new_posts:
        print("Новых вакансий нет — планировать нечего.")
        return

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        entity = await client.get_entity(CHANNEL)

        # Каждый следующий пост ставим на пару минут позже предыдущего,
        # чтобы они не выходили все одновременно одной пачкой.
        base_time = datetime.now(timezone.utc) + timedelta(hours=POST_DELAY_HOURS)
        for i, (vac_id, job_name, company_name, post_text) in enumerate(new_posts):
            schedule_time = base_time + timedelta(minutes=3 * i)
            await client.send_message(
                entity,
                post_text,
                parse_mode="html",
                schedule=schedule_time,
                link_preview=False,
            )
            seen.add(vac_id)
            print(f"  [scheduled] {job_name} — {company_name} -> {schedule_time.isoformat()}")

    save_seen(seen)
    print(f"\nГотово. Поставлено в отложку: {len(new_posts)}")


if __name__ == "__main__":
    asyncio.run(main())
