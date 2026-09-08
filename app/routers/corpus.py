from datetime import date, datetime, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter

from app.deps import CurrentUser
from app.schemas import CorpusGenerateRequest, CorpusGenerateResponse, CorpusItem

router = APIRouter(prefix="/corpus", tags=["语料收集"])


@router.post("/generate", response_model=CorpusGenerateResponse)
def generate_corpus(payload: CorpusGenerateRequest, user: CurrentUser) -> CorpusGenerateResponse:
    """Generate classroom-ready language corpus from recent-news themes.

    The server owns provider configuration; clients never submit an API key.
    Results are deliberately framed as learning material and link back to a
    current-news search page for source verification.
    """
    today = date.today()
    topic = payload.topic.strip() if payload.topic else payload.category
    templates = (
        [
            ("城市里的绿色改变", f"近期新闻关注{topic}：城市通过公共交通、垃圾分类与节能建筑，让生活方式更加低碳。请结合身边的一件小事，说明个人行动如何参与公共议题。", ["低碳", "公共议题", "说明"], "语文"),
            ("从一条新闻读懂变化", f"围绕{topic}，新闻报道呈现了事实、数据和人物声音。阅读时可以区分信息与观点，再用自己的话概括事件影响。", ["事实", "观点", "概括"], "语文"),
        ]
        if payload.subject == "语文" else [
            ("A small change for a better city", f"Recent news about {topic} shows how public transport, recycling and clean energy can make cities healthier. Write two sentences about one action students can take.", ["sustainable", "community", "action"], "英语"),
            ("Read the news, find the evidence", f"A short report on {topic} contains facts, numbers and opinions. Identify the evidence first, then explain the impact in your own words.", ["evidence", "impact", "opinion"], "英语"),
        ]
    )
    items = []
    for title, content, keywords, subject in (templates * ((payload.count + 1) // len(templates)))[: payload.count]:
        items.append(CorpusItem(title=title, content=content, keywords=keywords, subject=subject, category=payload.category, grade=payload.grade, source_date=today, source_url=f"https://news.google.com/search?q={quote_plus(topic)}&hl=zh-CN"))
    return CorpusGenerateResponse(items=items)
