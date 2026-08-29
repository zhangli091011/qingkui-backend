from app.services.wikibooks import _clean_rendered_html, _clean_wikitext, _is_high_school_math


def test_high_school_math_filter():
    assert _is_high_school_math("高中数学/函数与三角/函数的概念")
    assert not _is_high_school_math("高中数学/目录/函数与三角")
    assert not _is_high_school_math("Python/函数")


def test_wikitext_cleaner_keeps_readable_content():
    raw = """== 函数 ==\n[[定义域|定义域]]是变量的取值范围。<ref>来源</ref>\n{{模板}}\n[https://example.com 链接]"""
    cleaned = _clean_wikitext(raw)
    assert "定义域是变量的取值范围。" in cleaned
    assert "来源" not in cleaned
    assert "链接" in cleaned


def test_rendered_html_cleaner_ignores_decorative_images_and_keeps_math():
    html = """
    <h2>函数</h2><p><img alt="Crystal Clear app gnome" class="mw-file-element" />定义域。</p>
    <span class="mw-editsection">编辑</span><math><annotation>{\\displaystyle x^2}</annotation></math>
    <img alt="{\\displaystyle x^2}" class="mwe-math-fallback-image-inline" />
    """
    cleaned = _clean_rendered_html(html)
    assert "函数" in cleaned
    assert "定义域" in cleaned
    assert "x^2" in cleaned
    assert "displaystyle" not in cleaned
    assert "Crystal" not in cleaned
    assert "编辑" not in cleaned
    assert "displaystyle" not in cleaned
