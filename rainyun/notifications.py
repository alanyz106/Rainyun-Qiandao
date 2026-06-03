import logging

logger = logging.getLogger(__name__)


class NotificationProvider:
    """通知提供者基类"""
    MAX_BYTES = 0
    CONTENT_KEYS = []

    def send(self, title, context):
        raise NotImplementedError

    def select_content(self, context, max_bytes_override=None):
        limit = max_bytes_override if max_bytes_override is not None else self.MAX_BYTES

        for key in self.CONTENT_KEYS:
            content = context.get(key, '')
            if not content:
                continue
            byte_size = len(content.encode('utf-8'))
            if limit == 0 or byte_size <= limit:
                if key != self.CONTENT_KEYS[0]:
                    logging.info(f"{self.__class__.__name__}: 内容降级到 {key} ({byte_size} bytes)")
                return content

        last_key = self.CONTENT_KEYS[-1] if self.CONTENT_KEYS else ''
        last_content = context.get(last_key, '')
        if last_content and limit > 0:
            logging.warning(f"{self.__class__.__name__}: 所有内容版本均超限，执行安全截断")
            return self._safe_truncate(last_content, limit)
        return last_content

    @staticmethod
    def _safe_truncate(content, max_bytes):
        encoded = content.encode('utf-8')
        if len(encoded) <= max_bytes:
            return content
        suffix = '\n\n... [内容已截断]'
        suffix_bytes = len(suffix.encode('utf-8'))
        truncated = encoded[:max_bytes - suffix_bytes]
        return truncated.decode('utf-8', errors='ignore') + suffix


class PushPlusProvider(NotificationProvider):
    """PushPlus 推送渠道"""
    MAX_BYTES = 90_000
    FALLBACK_MAX_BYTES = 18_000
    CONTENT_KEYS = ['html_full', 'html_lite', 'summary_html']

    def __init__(self, token):
        self.token = token

    def send(self, title, context):
        import requests
        url = 'http://www.pushplus.plus/send'

        content = self.select_content(context)
        success = self._do_send(requests, url, title, content)

        if not success:
            logging.info("PushPlus: 推送失败，降级到实名用户限额 (2万字) 重试")
            content = self.select_content(context, max_bytes_override=self.FALLBACK_MAX_BYTES)
            success = self._do_send(requests, url, title, content)

        return success

    def _do_send(self, requests, url, title, content):
        data = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": "html"
        }
        try:
            logging.info(f"Sending PushPlus notification: {title} ({len(content.encode('utf-8'))} bytes)")
            response = requests.post(url, json=data, timeout=10)
            result = response.json()
            if result.get('code') == 200:
                logging.info("PushPlus notification sent successfully")
                return True
            else:
                logging.error(f"PushPlus notification failed: {result.get('msg')}")
                return False
        except Exception as e:
            logging.error(f"Error sending PushPlus notification: {e}")
            return False


class WXPusherProvider(NotificationProvider):
    """WXPusher 推送渠道"""
    MAX_BYTES = 36_000
    CONTENT_KEYS = ['html_full', 'html_lite', 'summary_html']

    def __init__(self, app_token, uids=None, topic_ids=None):
        self.app_token = app_token
        if uids:
            self.uids = uids if isinstance(uids, list) else [uid.strip() for uid in str(uids).split(',') if uid.strip()]
        else:
            self.uids = []

        if topic_ids:
            self.topic_ids = topic_ids if isinstance(topic_ids, list) else [tid.strip() for tid in str(topic_ids).split(',') if tid.strip()]
        else:
            self.topic_ids = []

    def send(self, title, context):
        import requests
        import time as _time
        content = self.select_content(context)
        url = 'https://wxpusher.zjiecode.com/api/send/message'
        data = {
            "appToken": self.app_token,
            "content": content,
            "summary": title,
            "contentType": 2,
            "uids": self.uids,
            "topicIds": self.topic_ids
        }
        target_desc = f"UIDs: {len(self.uids)}" if self.uids else ""
        if self.topic_ids:
            target_desc += (" & " if target_desc else "") + f"Topics: {len(self.topic_ids)}"

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logging.info(f"Sending WXPusher notification to {target_desc}: {title} ({len(content.encode('utf-8'))} bytes) (attempt {attempt}/{max_retries})")
                response = requests.post(url, json=data, timeout=30)
                result = response.json()
                if result.get('code') == 1000:
                    logging.info("WXPusher notification sent successfully")
                    return True
                else:
                    logging.error(f"WXPusher notification failed: {result.get('msg')}")
                    return False
            except Exception as e:
                logging.error(f"Error sending WXPusher notification (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    wait = attempt * 5
                    logging.info(f"Retrying in {wait}s...")
                    _time.sleep(wait)
        return False


class DingTalkProvider(NotificationProvider):
    """钉钉机器人推送渠道"""
    MAX_BYTES = 18_000
    CONTENT_KEYS = ['markdown_full', 'markdown_lite', 'summary_markdown']

    def __init__(self, access_token, secret=None):
        self.access_token = access_token
        self.secret = secret

    def send(self, title, context):
        import requests
        import time
        import hmac
        import hashlib
        import base64
        import urllib.parse

        content = self.select_content(context)
        md_text = f"# {title}\n\n{content}"

        url = 'https://oapi.dingtalk.com/robot/send'
        params = {'access_token': self.access_token}

        if self.secret:
            timestamp = str(round(time.time() * 1000))
            secret_enc = self.secret.encode('utf-8')
            string_to_sign = '{}\n{}'.format(timestamp, self.secret)
            string_to_sign_enc = string_to_sign.encode('utf-8')
            hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            params['timestamp'] = timestamp
            params['sign'] = sign

        data = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": md_text
            }
        }

        try:
            logging.info(f"Sending DingTalk notification: {title} ({len(md_text.encode('utf-8'))} bytes)")
            response = requests.post(url, params=params, json=data, timeout=10)
            result = response.json()
            if result.get('errcode') == 0:
                logging.info("DingTalk notification sent successfully")
                return True
            else:
                logging.error(f"DingTalk notification failed: {result.get('errmsg')}")
                return False
        except Exception as e:
            logging.error(f"Error sending DingTalk notification: {e}")
            return False


class EmailProvider(NotificationProvider):
    """邮件推送渠道"""
    MAX_BYTES = 0
    CONTENT_KEYS = ['html_email', 'html_full']

    def __init__(self, host, port, user, password, to_email):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.to_email = to_email

    def send(self, title, context):
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.header import Header

        content = self.select_content(context)

        try:
            message = MIMEMultipart()
            message['From'] = f"Rainyun-Qiandao <{self.user}>"
            message['To'] = self.to_email
            message['Subject'] = Header(title, 'utf-8')

            message.attach(MIMEText(content, 'html', 'utf-8'))

            logging.info(f"Sending Email notification to {self.to_email}")

            if self.port == 465:
                server = smtplib.SMTP_SSL(self.host, self.port)
            else:
                server = smtplib.SMTP(self.host, self.port)
                try:
                    server.starttls()
                except:
                    pass

            server.login(self.user, self.password)
            server.sendmail(self.user, [self.to_email], message.as_string())
            server.quit()

            logging.info("Email notification sent successfully")
            return True
        except Exception as e:
            logging.error(f"Error sending Email notification: {e}")
            return False


class NotificationManager:
    """通知管理器"""
    def __init__(self):
        self.providers = []

    def add_provider(self, provider):
        self.providers.append(provider)

    def send_all(self, title, context):
        if not self.providers:
            logging.info("No notification providers configured.")
            return

        logging.info(f"Sending notifications to {len(self.providers)} providers...")
        for provider in self.providers:
            provider.send(title, context)
