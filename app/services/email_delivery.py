from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


def send_password_reset_email(address: str, token: str) -> None:
    if settings.email_provider == "console":
        return
    if settings.email_provider != "smtp" or not settings.smtp_host or not settings.smtp_from_address:
        raise RuntimeError("邮件服务尚未配置")
    message = EmailMessage()
    message["Subject"] = "青葵计划密码重置验证码"
    message["From"] = settings.smtp_from_address
    message["To"] = address
    message.set_content(
        "你正在重置青葵计划账户密码。请在应用中输入以下一次性令牌：\n\n"
        f"{token}\n\n令牌将在 {settings.password_reset_minutes} 分钟后过期。若非本人操作，请忽略本邮件。"
    )
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        if settings.smtp_starttls:
            client.starttls()
        if settings.smtp_username and settings.smtp_password:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)
