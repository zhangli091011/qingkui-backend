from app.services.metadata import infer_chapter, infer_grade, infer_textbook_version, resolve_document_grade


def test_launch_textbook_metadata_is_canonicalized() -> None:
    path = "E:/资料/高中数学必修第一册（人教A版）/第01讲 1.1集合的概念（教师版）.docx"

    assert infer_grade(path) == "高一"
    assert infer_textbook_version(path) == "人教A版"


def test_selective_compulsory_topics_are_not_classified_as_grade_ten() -> None:
    assert infer_grade("专题4.12 第四章 数列（人教A版选择性必修第二册）") == "高二"
    assert infer_grade("第01讲 5.1导数的概念及其几何意义（教师版）") == "高二"


def test_strong_source_evidence_repairs_stale_grade_ten_metadata() -> None:
    source = "E:/资料/高中数学选择性必修第一册（人教A版）/第01讲 2.1.1倾斜角与斜率（教师版）.docx"

    assert resolve_document_grade(
        "高一",
        title="第01讲 2.1.1倾斜角与斜率（教师版）",
        haystack=source,
        metadata_source="explicit_import",
    ) == "高二"


def test_numbered_lecture_title_becomes_stable_chapter() -> None:
    title = "第01讲 3.1.1函数的概念（知识清单+15类热点题型讲练）（教师版）.docx"

    assert infer_chapter(title, {"filename": title}) == "3.1.1 函数的概念"


def test_explicit_title_is_not_masked_by_unhelpful_filename() -> None:
    metadata = {"filename": "扫描件_001.pdf"}

    assert infer_chapter("第08讲 拓展二：直线与平面所成角（教师版）", metadata) == "直线与平面所成角"


def test_special_topic_and_full_book_formula_titles_are_inferred() -> None:
    assert infer_chapter("专题7.10 随机变量及其分布（知识清单）") == "7.10 随机变量及其分布"
    assert infer_chapter("高中数学全册公式OCR汇总.pdf") == "全册公式索引"
    assert infer_chapter("高中数学必修第一册（人教A版）·公式OCR") == "全册公式索引"
