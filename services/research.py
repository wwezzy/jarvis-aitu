"""Optional, real Tavily search; local personal facts never enter its query."""
import os
import re
from typing import Protocol

import aiohttp

from services.lms import safe_url


class ResearchProvider(Protocol):
    async def search(self, query: str) -> list[dict]: ...


class TavilyResearch:
    async def search(self, query):
        key = os.getenv('WEB_RESEARCH_API_KEY', '')
        if not key:
            raise RuntimeError('Research provider is unconfigured')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post('https://api.tavily.com/search', headers={'Authorization': f'Bearer {key}'},
                json={'query': query[:1000], 'max_results': 5, 'search_depth': 'basic',
                      'include_answer': False, 'include_raw_content': False}) as response:
                if response.status != 200:
                    raise RuntimeError('Research provider unavailable')
                data = await response.json()
        return [{'title': str(row.get('title') or 'Source')[:200], 'url': safe_url(row.get('url')),
                 'excerpt': str(row.get('content') or '')[:1800]} for row in data.get('results', [])[:5]]


def needs_freshness(text):
    lower = text.lower()
    # Never route personal schedule, LMS or memory queries to the public web.
    if re.search(r'\b(my|мой|моя|моё|мое|мои|lms|дедлайн|расписание|память|memory)\b', lower):
        return False
    return bool(re.search(r'\b(latest|current|today|news|сегодня|новости|последни\w*|актуальн\w*)\b', lower))


async def research(text, *, provider=None):
    query = text.removeprefix('/research').strip()
    if not needs_freshness(query):
        return 'Веб-поиск предназначен для актуальных публичных сведений. Личные факты доступны через /tasks, /today и /memory.'
    if os.getenv('ENABLE_WEB_RESEARCH', '').lower() != 'true' or not os.getenv('WEB_RESEARCH_API_KEY'):
        return 'Веб-исследование недоступно: провайдер выключен или не настроен.'
    try:
        results = await (provider or TavilyResearch()).search(query)
    except Exception:
        return 'Веб-исследование временно недоступно. Личные данные и команды работают.'
    lines = ['Актуальные внешние источники · выдержки поиска, не личные факты:']
    for item in results:
        url = safe_url(item.get('url'))
        if url:
            lines.append(f"{item['title']}\n{item['excerpt']}\nИсточник: {url}")
    return '\n\n'.join(lines) if len(lines) > 1 else 'Проверяемых источников не найдено.'
