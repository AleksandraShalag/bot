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
import io
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timedelta, timezone
from jinja2 import Template
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import FloodWaitError

COMPANIES_FILE = "companies.yaml"
TEMPLATE_FILE = "template.md"
SEEN_FILE = "seen_ids.json"

API_BASE = "https://trudvsem.ru"
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

# ---------- Картинка ----------

IMG_SIZE = (1080, 1080)
IMG_BG = "#1F3A5F"
IMG_TEXT = "#FFFFFF"
IMG_ACCENT = "#FFD54A"
IMG_PADDING = 80

FONT_BOLD_CANDIDATES = [
    "fonts/bold.ttf",  # свой шрифт, если положите в репозиторий
    "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

CITY_RE = re.compile(
    r"^(?:г|город|пгт|рп|п|пос|поселок|посёлок|с|село|д|деревня)\.?\s+(.+)$",
    re.IGNORECASE,
)
CITY_RE_TAIL = re.compile(
    r"^(.+?)\s+(?:г|город|пгт|рп|п|с|д)\.?$",
    re.IGNORECASE,
)
SKIP_PARTS = ("респ", "обл", "край", "округ", "р-н", "район", "ул", "пр-кт", "проспект", "д.")


def extract_city(v: dict) -> str:
    """Город/населённый пункт из адреса, иначе регион."""
    address = get_address(v) or ""
    for part in [p.strip() for p in address.split(",") if p.strip()]:
        low = part.lower()
        m = CITY_RE.match(part) or CITY_RE_TAIL.match(part)
        if m:
            name = m.group(1).strip(" ,.")
            if name and not any(low.startswith(s) for s in ("ул", "пр-кт")):
                return name
    region = (v.get("region") or {}).get("name") or ""
    return region.split("(")[0].strip()

def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_BOLD_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    raise RuntimeError("Не найден шрифт с кириллицей для генерации картинки")


def wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        test = f"{line} {word}".strip()
        if draw.textlength(test, font=font) <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit_block(draw, text, max_size, min_size, max_lines, max_width):
    """Подбирает самый крупный размер шрифта, при котором текст влезает."""
    for size in range(max_size, min_size - 1, -4):
        font = load_font(size)
        lines = wrap_text(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
    font = load_font(min_size)
    return font, wrap_text(draw, text, font, max_width)[:max_lines]


def generate_image(v: dict) -> io.BytesIO:
    img = Image.new("RGB", IMG_SIZE, IMG_BG)
    draw = ImageDraw.Draw(img)
    max_w = IMG_SIZE[0] - 2 * IMG_PADDING

    title = str(v.get("job-name", "Без названия")).upper()
    city = extract_city(v).upper()
    company = str(v.get("_company_display_name") or (v.get("company") or {}).get("name", "")).upper()
    salary = (format_salary(v) or "").upper()

    blocks = []  # (шрифт, строки, цвет, отступ после блока)
    f, l = fit_block(draw, title, 110, 52, 5, max_w)
    blocks.append((f, l, IMG_TEXT, 60))
    if city:
        f, l = fit_block(draw, city, 56, 36, 2, max_w)
        blocks.append((f, l, IMG_TEXT, 36))
    if company:
        f, l = fit_block(draw, company, 48, 30, 3, max_w)
        blocks.append((f, l, IMG_TEXT, 60))
    if salary:
        f, l = fit_block(draw, salary, 80, 40, 2, max_w)
        blocks.append((f, l, IMG_ACCENT, 0))

    def line_h(font):
        b = font.getbbox("АЙgу")
        return int((b[3] - b[1]) * 1.25)

    total = sum(line_h(f) * len(l) + gap for f, l, _, gap in blocks)
    y = (IMG_SIZE[1] - total) // 2
    for font, lines, color, gap in blocks:
        for line in lines:
            draw.text((IMG_PADDING, y), line, font=font, fill=color)
            y += line_h(font)
        y += gap

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "vacancy.png"
    return buf
# ---------- seen_ids ----------

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                return set()
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)

import time

API_BASE = "https://opendata.trudvsem.ru/api/v1/vacancies/company"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; vacancy-bot/1.0)",
    "Accept": "application/json",
}
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

def _get_with_retry(client, url, params, attempts=4):
    """GET с повтором при 429/5xx/сетевых сбоях и растущей паузой."""
    for attempt in range(attempts):
        try:
            resp = client.get(url, params=params)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"retryable {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if attempt == attempts - 1:
                raise
            wait = 5 * (2 ** attempt)
            print(f"  [retry] {e}; повтор через {wait}с")
            time.sleep(wait)


def fetch_company_vacancies(company: dict) -> list[dict]:
    id_type = company["id_type"]
    company_id = re.sub(r"\s+", "", str(company["id"]))
    url = f"{API_BASE}/{id_type}/{company_id}"

    all_vacancies = []
    offset = 0
    with httpx.Client(timeout=30, headers=HEADERS) as client_http:
        while True:
            try:
                data = _get_with_retry(client_http, url, {"limit": PAGE_LIMIT, "offset": offset})
            except Exception as e:
                print(f"  [error] Ошибка запроса к API для {company_id}: {e}")
                break

            vacancies = (data.get("results") or {}).get("vacancies") or []
            if not vacancies:
                break
            for item in vacancies:
                v = item.get("vacancy")
                if v:
                    all_vacancies.append(v)

            if len(vacancies) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
            time.sleep(1)
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

    

    return {
        "phone": phone,
        "email": email,
        "contact_person": v.get("contact_person"),
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
        employment_label=EMPLOYMENT_LABELS.get(employment_key, "занятость не указана"),
        schedule=v.get("schedule"),
        experience_label=experience_label,
        salary_text=format_salary(v),
        duties=duties,
        benefits=format_benefits(v),
        phone=contacts["phone"],
        email=contacts["email"],
        contact_person=contacts["contact_person"],
        vac_url=v.get("vac_url", ""),
        tags=[f"#{t}" for t in sorted(tags)],
    )
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


# ---------- Основной сценарий ----------

async def main():
    if not os.path.exists(COMPANIES_FILE) or not os.path.exists(TEMPLATE_FILE):
        print("[error] Отсутствует файл companies.yaml или template.md")
        return

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
        new_posts.append(
            (vac_id, v.get("job-name"), v.get("_company_display_name"), post_text, v)
        )

    print(f"Новых вакансий для публикации: {len(new_posts)}")
    if not new_posts:
        print("Новых вакансий нет — планировать нечего.")
        return

    print("[telegram] Подключение к Telegram API...")
    scheduled_count = 0

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        entity = await client.get_entity(CHANNEL)

        # Каждый следующий пост сдвигаем на 3 минуты, чтобы они выходили постепенно
        base_time = datetime.now(timezone.utc) + timedelta(hours=POST_DELAY_HOURS)

        for i, (vac_id, job_name, company_name, post_text, vac) in enumerate(new_posts):
            schedule_time = base_time + timedelta(minutes=3 * i)

            # Безопасный цикл отправки с ожиданием при лимитах флуда
            while True:
                try:
                    image = generate_image(vac)

                    if len(post_text) <= 1024:
                        # Лимит подписи к фото в Telegram — 1024 символа
                        await client.send_file(
                            entity,
                            image,
                            caption=post_text,
                            parse_mode="html",
                            schedule=schedule_time,
                        )
                    else:
                        # Не влезает в подпись: сначала картинка, через минуту текст
                        await client.send_file(
                            entity,
                            image,
                            schedule=schedule_time,
                        )
                        await asyncio.sleep(2)
                        await client.send_message(
                            entity,
                            post_text,
                            parse_mode="html",
                            schedule=schedule_time + timedelta(minutes=1),
                            link_preview=False,
                        )

                    seen.add(vac_id)
                    scheduled_count += 1
                    print(f"  [scheduled] {job_name} — {company_name} -> {schedule_time.isoformat()}")

                    # Защитная микропауза между запросами к API Telegram
                    await asyncio.sleep(4)
                    break

                except FloodWaitError as e:
                    print(f"  [flood] Превышен лимит запросов Telegram. Спим {e.seconds} секунд...")
                    await asyncio.sleep(e.seconds + 2)
                except Exception as e:
                    print(f"  [error] Ошибка отправки для ID {vac_id}: {e}")
                    break

    save_seen(seen)
    print(f"\nУспешно. Поставлено в отложку: {scheduled_count} из {len(new_posts)}")


if __name__ == "__main__":
    asyncio.run(main())
