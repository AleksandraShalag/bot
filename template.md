🔥 <b>{{ title }}</b>
🏢 {{ company }}
📍 {{ region }}{% if address %}, {{ address }}{% endif %}{% if remote %} · 🏠 удалённо{% endif %}
💼 {{ employment_label }}{% if schedule %} · {{ schedule }}{% endif %}{% if experience_label %} · {{ experience_label }}{% endif %}
{% if salary_text %}💰 {{ salary_text }}
{% endif %}
{% if description %}
{{ description }}
{% endif %}
{% if benefits %}✅ Условия: {{ benefits }}
{% endif %}
{{ tags | join(' ') }}

<b>Контакты:</b>
{% if contact_person %}👤 {{ contact_person }}
{% endif %}{% if phone %}📞 {{ phone }}
{% endif %}{% if email %}✉️ {{ email }}
{% endif %}{% if company_website %}🌐 Сайт компании: {{ company_website }}
{% endif %}
🔗 Подробнее о вакансии: {{ vac_url }}
