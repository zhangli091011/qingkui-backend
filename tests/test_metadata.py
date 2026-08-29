from app.services.metadata import infer_chapter, infer_grade, infer_textbook_version


def test_launch_textbook_metadata_is_canonicalized() -> None:
    path = "E:/资料/高中数学必修第一册（人教A版）/第01讲 1.1集合的概念（教师版）.docx"

    assert infer_grade(path) == "高一"
    assert infer_textbook_version(path) == "人教A版"


def test_selective_compulsory_topics_are_not_classified_as_grade_ten() -> None:
    assert infer_grade("专题4.12 第四章 数列（人教A版选择性必修第二册）") == "高二"
    assert infer_grade("第01讲 5.1导数的概念及其几何意义（教师版）") == "高二"


def test_numbered_lecture_title_becomes_stable_chapter() -> None:
    title = "第01讲 3.1.1函数的概念（知识清单+15类热点题型讲练）（教师版）.docx"

    assert infer_chapter(title, {"filename": title}) == "3.1.1 函数的概念"
