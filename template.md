🔥 <b>{{ title }}</b>
🏢 {{ company }}
📍 {{ region }}{% if remote %} · 🏠 удалённо{% endif %}
💼 {{ employment_label }}{% if experience_label %} · {{ experience_label }}{% endif %}
{% if salary_text %}💰 {{ salary_text }}
{% endif %}
{{ tags | join(' ') }}

🔗 {{ url }}
