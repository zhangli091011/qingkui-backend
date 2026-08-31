from app.services.content_safety import moderate_text


def test_personal_contact_and_identity_data_are_blocked() -> None:
    samples = {
        "personal_mobile": "我的手机号：13812345678",
        "personal_identity_number": "身份证 37020220080101123X",
        "personal_email": "联系我：student@example.com",
        "personal_address": "家庭住址：山东省青岛市市南区香港中路12号",
    }

    for category, value in samples.items():
        decision = moderate_text(value)
        assert decision.allowed is False
        assert decision.category == category


def test_ordinary_math_numbers_and_example_email_are_not_false_positives() -> None:
    samples = (
        "计算 13812345678 除以 2 的余数。",
        "身份证号码共有18位属于信息技术常识。",
        "集合 A={2026083112345, 2026090112345}。",
        "电子邮件地址的通用示例是 student@example.com。",
    )

    assert all(moderate_text(value).allowed for value in samples)
