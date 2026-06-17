import calendar
import json
import logging
import os
import time

from rainyun.config import now_local

logger = logging.getLogger(__name__)

BASE64_ICONS = {
    'coin': 'data:image/svg+xml;base64,PHN2ZyBjbGFzcz0iaWNvbiIgdmlld0JveD0iMCAwIDExMTQgMTAyNCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIiB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCI+PHBhdGggZD0iTTgwNy41MTEgNDAwLjY2NmE1MTIgNTEyIDAgMCAwLTYwLjE1LTUzLjg3M2MtMy4wNzItMi4zNDUtNS40MjctMy45ODMtOC4xNS01Ljk4IDM4LjA2Ni0xMy4wNzcgNjQuNy00NC4zOCA2NC43LTgxLjQzNCAwLTQ5LjktNDcuMzctODguMDgtMTAzLjYxOC04OC4wOGE5OS40IDk5LjQgMCAwIDAtMzUuNTU4IDYuNDk4IDc5IDc5IDAgMCAwLTExLjc3MSA1LjU5MWMtMS45NjYuODMtNi4xNi0wLjA5Ny03LjMxMi0xLjUzbC0uMDUuMDM1Yy00LjI5MS02LjQzLTEwLjc2My0xNC40MDItMjAuMTY4LTIyLjU2OS0xNy45LTE1LjU1NC0zOS4wOTItMjUuMTUtNjMuMjk0LTI1LjE1cy00NS4zODQgOS41OTYtNjMuMjg4IDI1LjE1Yy05LjE5IDcuOTc3LTE1LjQ5OCAxNS43MTMtMTkuODA0IDIyLjA3OGwtLjAyNi0uMDJjLTEuNjI4IDEuOTItNS44NTIgMi45MjgtNy4zMjIgMi4yMjFhNzguNCA3OC40IDAgMCAwLTEyLjE0NC01LjgxMSA5OS41IDk5LjUgMCAwIDAtMzUuNTY0LTYuNTAyYy01Ni4yNDggMC0xMDMuNjEzIDM4LjE4NS0xMDMuNjEzIDg4LjA3OSAwIDMxLjY4MyAxOS41NDMgNTkuMTA1IDQ4Ljk1NyA3NC42MjRhNDk1IDQ5NSAwIDAgMC05LjQwNSA2Ljg0IDQ2OCA0NjggMCAwIDAtNjAuMDU4IDUzLjMxNUMyNDQuMjY1IDQ1Mi45NTYgMjEwLjUgNTIwLjIxMiAyMTAuNSA1OTQuODcyYzAgMjA3LjAyMiAxNTQuMjggMzA1LjQ4IDM0MC4xMzEgMzA1LjQ4IDc3Ljg5MSAwIDE1NC4wMy0xNS41NCAyMTUuNjQtNTIuMjE5IDgzLjU5OS00OS43OTIgMTMxLjE1My0xMzMuNDI3IDEzMS4xNTMtMjUzLjI2LS4wMTUtNzAuMTY1LTMzLjk5Ni0xMzUuMzQ4LTg5LjkxMi0xOTQuMjA3TTY0Ni41NjQgNjAxLjQzYzEwLjU5OCAwIDE5LjE4NCA4Ljc5MSAxOS4xODQgMTkuNjE1IDAgMTAuODI5LTguNTkgMTkuNjI1LTE5LjE4NCAxOS42MjVINTY5LjgxdjU2LjQ4OWMwIDguMjg5LTguNTkxIDE1LjAwNi0xOS4xODUgMTUuMDA2LTEwLjU5OCAwLTE5LjE4NC02LjcxNy0xOS4xODQtMTUuMDA2di01Ni40OWgtNzYuNzU0Yy0xMC41OTkgMC0xOS4xODUtOC43OS0xOS4xODUtMTkuNjJzOC41OTEtMTkuNjE0IDE5LjE4NS0xOS42MTRoNzYuNzU0VjU4MS44MmgtNzYuNzU0Yy0xMC41OTkgMC0xOS4xODUtOC43ODUtMTkuMTg1LTE5LjYxNHM4LjU5MS0xOS42MTUgMTkuMTg1LTE5LjYxNWg3OC4zOTdsLTcyLjc4LTc0LjM5OWExOS45MTcgMTkuOTE3IDAgMCAxIDAtMjcuNzM1IDE4Ljg5MyAxOC44OTMgMCAwIDEgMjcuMTM1IDBsNjMuMTg2IDY0LjU4NCA2My4xODYtNjQuNTg0YTE4LjkwMyAxOC45MDMgMCAwIDEgMjYuNzIxLS40MjVsLjQyLjQyNWExOS45MjcgMTkuOTI3IDAgMCAxIDAgMjcuNzM1bC03Mi43OCA3NC4zOTloNzguNDAyYzEwLjU5OCAwIDE5LjE4IDguNzggMTkuMTggMTkuNjE1cy04LjU4NyAxOS42MTQtMTkuMTggMTkuNjE0aC03Ni43NTl2MTkuNjF6IiBmaWxsPSIjZjU5ZTBiIi8+PC9zdmc+'
}


def get_screenshot_html(screenshot_path):
    if not screenshot_path or not os.path.exists(screenshot_path):
        return ""

    try:
        import base64
        with open(screenshot_path, "rb") as img_file:
            img_data = base64.b64encode(img_file.read()).decode('utf-8')

        mime_type = "image/jpeg" if screenshot_path.lower().endswith(('.jpg', '.jpeg')) else "image/png"
        file_size = os.path.getsize(screenshot_path) / 1024

        return f'''
            <div style="margin-top: 12px; border-top: 1px solid var(--border); padding-top: 12px;">
                <div style="font-size: 12px; color: var(--text-sub); margin-bottom: 8px;">📸 截图 ({file_size:.1f}KB)</div>
                <img src="data:{mime_type};base64,{img_data}" style="max-width: 100%; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);" alt="签到截图"/>
            </div>
        '''
    except Exception as e:
        logger.debug(f"生成截图 HTML 时出错: {e}")
        return ""


def generate_html_report(results, screenshot_mode='all'):
    now_str = now_local().strftime('%Y-%m-%d %H:%M:%S')
    success_count = len([r for r in results if r['status']])
    total_count = len(results)

    style_block = """
    <style>
        :root {
            --bg-body: #f9fafb;
            --bg-card: #ffffff;
            --text-main: #111827;
            --text-sub: #6b7280;
            --border: #e5e7eb;
            --bg-success: #ecfdf5;
            --text-success: #059669;
            --bg-error: #fef2f2;
            --text-error: #dc2626;
            --bg-footer: #f3f4f6;
            --text-footer: #9ca3af;
        }
        @media (prefers-color-scheme: dark) {
            :root {
                --bg-body: #18181b;
                --bg-card: #27272a;
                --text-main: #f3f4f6;
                --text-sub: #9ca3af;
                --border: #3f3f46;
                --bg-success: #064e3b;
                --text-success: #34d399;
                --bg-error: #7f1d1d;
                --text-error: #f87171;
                --bg-footer: #1f2937;
                --text-footer: #6b7280;
            }
        }
        .container { max-width: 600px; margin: 0 auto; background-color: var(--bg-body); border-radius: 16px; overflow: hidden; border: 1px solid var(--border); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06); }
        .header { background-color: var(--bg-card); padding: 24px; border-bottom: 1px solid var(--border); }
        .title { margin: 0; color: var(--text-main); font-size: 20px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
        .subtitle { margin-top: 8px; color: var(--text-sub); font-size: 13px; font-weight: 500;}
        .badges { margin-top: 16px; display: flex; gap: 8px; }
        .badge-success { background-color: var(--bg-success); color: var(--text-success); padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; }
        .badge-error { background-color: var(--bg-error); color: var(--text-error); padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; }
        .content { padding: 16px; background-color: var(--bg-body); }
        .card { background-color: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 12px; box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px 0 rgba(0, 0, 0, 0.06); }
        .row-item { display: flex; align-items: center; gap: 6px; }
        .footer { background-color: var(--bg-body); padding: 20px; text-align: center; font-size: 12px; color: var(--text-footer); }
        svg { width: 20px; height: 20px; display: block; }
        .icon-img { width: 20px; height: 20px; vertical-align: middle; display: inline-block; }
    </style>
    """

    html = f"""
    {style_block}
    <div class="container">
        <div class="header">
            <h3 class="title">
                🌧️ 雨云签到报告
            </h3>
            <div class="subtitle">
                {now_str}
            </div>
            <div class="badges">
                <span class="badge-success">
                    成功: {success_count}
                </span>
                <span class="badge-error">
                    失败: {total_count - success_count}
                </span>
            </div>
        </div>

        <div class="content">
    """

    for res in results:
        status_color = "var(--text-success)" if res['status'] else "var(--text-error)"
        status_bg = "var(--bg-success)" if res['status'] else "var(--bg-error)"

        points_element = ""
        if res.get('points'):
            points = res['points']
            money = points / 2000
            points_element = f"""
            <div class="row-item" style="color: #f59e0b; font-weight: 500;">
                <img src="{BASE64_ICONS['coin']}" class="icon-img" alt="coin" />
                <span>{points} (≈￥{money:.2f})</span>
            </div>
            """
        else:
            points_element = f"""
            <div class="row-item" style="color: var(--text-error);">
               <span>{res['msg']}</span>
            </div>
            """

        html += f"""
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div class="row-item" style="font-weight: 600; font-size: 15px;">
                    <span>{res['username']}</span>
                </div>
                <span style="background-color: {status_bg}; color: {status_color}; padding: 2px 8px; border-radius: 6px; font-size: 12px; font-weight: 600;">
                    {'今日已签到' if res['status'] and res.get('msg') == '今日已签到' else ('签到成功' if res['status'] else '签到失败')}
                </span>
            </div>

            <div style="height: 1px; background-color: var(--border); margin-bottom: 12px; opacity: 0.5;"></div>

            <div style="display: flex; justify-content: space-between; align-items: center; font-size: 13px;">
                {points_element}
                <div class="row-item" style="color: var(--text-sub); font-size: 12px;">
                    <span>重试: {res.get('retries', 0)}</span>
                </div>
            </div>
            {get_screenshot_html(res.get('screenshot')) if screenshot_mode == 'all' or (screenshot_mode == 'failed_only' and not res['status']) else ''}
        </div>
        """

    # 月统计块
    monthly_html = generate_monthly_stats_html()
    if monthly_html:
        html += f"""
        <div style="padding: 0 16px 16px 16px; background-color: var(--bg-body);">
            {monthly_html}
        </div>
        """

    html += """
        </div>
        <div class="footer">
            Powered by Rainyun-Qiandao
        </div>
    </div>
    """
    return html


def generate_markdown_report(results, compact=False):
    now_str = now_local().strftime('%Y-%m-%d %H:%M:%S')
    success_count = len([r for r in results if r['status']])
    total_count = len(results)

    md = f"> {now_str}\n\n"
    md += f"**状态**: ✅ {success_count} 成功 / ❌ {total_count - success_count} 失败\n\n"
    md += "---\n"

    for res in results:
        status_icon = "✅" if res['status'] else "❌"
        status_text = "今日已签到" if res['status'] and res.get('msg') == '今日已签到' else ("签到成功" if res['status'] else "签到失败")

        if compact and res['status']:
            points_str = f" | {res['points']}积分" if res.get('points') else ""
            md += f"- {status_icon} {res['username']}{points_str}\n"
        else:
            md += f"### {status_icon} {res['username']} ({status_text})\n"

            if res.get('points'):
                points = res['points']
                money = points / 2000
                md += f"- **积分**: {points} (≈￥{money:.2f})\n"

            md += f"- **消息**: {res['msg']}\n"
            if res.get('retries', 0) > 0:
                md += f"- **重试**: {res['retries']}\n"
            md += "\n"

    md += "---\n"
    md += "Powered by Rainyun-Qiandao"
    md += generate_monthly_stats_markdown()
    return md


def generate_summary_report(results, fmt='html'):
    now_str = now_local().strftime('%Y-%m-%d %H:%M:%S')
    success_count = len([r for r in results if r['status']])
    fail_count = len(results) - success_count
    total_count = len(results)

    if fmt == 'html':
        lines = []
        lines.append(f'<div style="font-family: sans-serif; padding: 16px;">')
        lines.append(f'<h3>🌧️ 雨云签到摘要</h3>')
        lines.append(f'<p style="color: #6b7280; font-size: 13px;">{now_str}</p>')
        lines.append(f'<p><b>✅ 成功: {success_count}</b> / <b>❌ 失败: {fail_count}</b> / 共 {total_count}</p>')
        lines.append('<hr>')

        for res in results:
            icon = '✅' if res['status'] else '❌'
            detail = ''
            if res['status'] and res.get('msg') == '今日已签到':
                detail = f" — 今日已签到"
            elif res['status'] and res.get('points'):
                detail = f" — {res['points']}积分"
            elif not res['status']:
                detail = f" — {res['msg']}"
                if res.get('retries', 0) > 0:
                    detail += f" (重试{res['retries']}次)"
            lines.append(f'<p>{icon} {res["username"]}{detail}</p>')

        lines.append('<hr>')
        lines.append('<p style="font-size: 12px; color: #9ca3af;">Powered by Rainyun-Qiandao</p>')
        lines.append('</div>')
        return '\n'.join(lines)
    else:
        lines = []
        lines.append(f'> {now_str}')
        lines.append(f'')
        lines.append(f'**✅ 成功: {success_count}** / **❌ 失败: {fail_count}** / 共 {total_count}')
        lines.append('---')

        for res in results:
            icon = '✅' if res['status'] else '❌'
            detail = ''
            if res['status'] and res.get('msg') == '今日已签到':
                detail = f" — 今日已签到"
            elif res['status'] and res.get('points'):
                detail = f" — {res['points']}积分"
            elif not res['status']:
                detail = f" — {res['msg']}"
                if res.get('retries', 0) > 0:
                    detail += f" (重试{res['retries']}次)"
            lines.append(f'- {icon} {res["username"]}{detail}')

        lines.append('---')
        lines.append('Powered by Rainyun-Qiandao')
        return '\n'.join(lines)


def save_screenshot(driver, account_id, status="success", error_msg=""):
    try:
        screenshot_dir = os.path.abspath(os.path.join("temp", "screenshots"))
        os.makedirs(screenshot_dir, exist_ok=True)

        timestamp = now_local().strftime("%Y%m%d_%H%M%S")
        masked_account = f"{account_id[:3]}xxx{account_id[-3:] if len(account_id) > 6 else account_id}"

        temp_filepath = os.path.join(screenshot_dir, f"temp_{timestamp}.png")
        if not driver.save_screenshot(temp_filepath):
            logger.error(f"无法保存截图到: {temp_filepath}")
            return None

        if not os.path.exists(temp_filepath):
            logger.error(f"截图文件未创建: {temp_filepath}")
            return None

        compressed_filename = f"{status}_{masked_account}_{timestamp}.jpg"
        compressed_filepath = os.path.join(screenshot_dir, compressed_filename)

        original_size = os.path.getsize(temp_filepath)
        compressed_size = compress_screenshot(temp_filepath, compressed_filepath)

        try:
            os.remove(temp_filepath)
        except:
            pass

        if compressed_size:
            compression_ratio = (1 - compressed_size / original_size) * 100
            status_text = "成功" if status == "success" else "失败"
            logger.info(f"已保存{status_text}截图: {compressed_filepath} (压缩率: {compression_ratio:.1f}%, {original_size/1024:.1f}KB -> {compressed_size/1024:.1f}KB)")

            cleanup_old_screenshots(screenshot_dir, days=7)

            return compressed_filepath
        else:
            logger.warning("截图压缩失败，使用原始文件")
            return temp_filepath

    except Exception as e:
        logger.error(f"保存截图时出错: {e}")
        return None


def compress_screenshot(input_path, output_path, max_width=1920, quality=85):
    result = compress_with_pillow(input_path, output_path, max_width, quality)
    if not result:
        return None

    tinypng_key = os.getenv("TINYPNG_API_KEY", "").strip()
    if tinypng_key:
        tinypng_result = compress_with_tinypng(output_path, output_path, tinypng_key)
        return tinypng_result or result

    return result


def compress_with_tinypng(input_path, output_path, api_key):
    import requests
    import base64

    try:
        if os.path.getsize(input_path) > 5 * 1024 * 1024:
            logger.warning("图片超过 TinyPNG 5MB 限制")
            return None

        with open(input_path, "rb") as f:
            image_data = f.read()

        auth = base64.b64encode(f"api:{api_key}".encode()).decode()
        resp = requests.post(
            "https://api.tinify.com/shrink",
            headers={"Authorization": f"Basic {auth}"},
            data=image_data,
            timeout=30
        )

        if resp.status_code != 201:
            error_map = {401: "API Key 无效", 429: "本月额度已用完"}
            logger.warning(f"TinyPNG: {error_map.get(resp.status_code, resp.status_code)}")
            return None

        compressed_url = resp.json().get("output", {}).get("url")
        if not compressed_url:
            return None

        img_resp = requests.get(compressed_url, timeout=30)
        if img_resp.status_code != 200:
            return None

        with open(output_path, "wb") as f:
            f.write(img_resp.content)

        used = resp.headers.get("Compression-Count", "?")
        logger.info(f"TinyPNG 压缩成功 (已用: {used}/500)")
        return os.path.getsize(output_path)

    except Exception as e:
        logger.debug(f"TinyPNG 出错: {e}")
        return None


def compress_with_pillow(input_path, output_path, max_width=1920, quality=85):
    try:
        from PIL import Image

        with Image.open(input_path) as img:
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            w, h = img.size
            if w > max_width:
                img = img.resize((max_width, int(h * max_width / w)), Image.Resampling.LANCZOS)

            img.save(output_path, 'JPEG', quality=quality, optimize=True)

        return os.path.getsize(output_path)
    except Exception as e:
        logger.debug(f"Pillow 压缩出错: {e}")
        return None


def cleanup_old_screenshots(screenshot_dir, days=7):
    try:
        now = time.time()
        cutoff = now - (days * 86400)

        for filename in os.listdir(screenshot_dir):
            file_path = os.path.join(screenshot_dir, filename)
            if os.path.isfile(file_path) and (filename.endswith('.png') or filename.endswith('.jpg')):
                if filename.startswith('success_') or filename.startswith('failure_'):
                    file_time = os.path.getmtime(file_path)
                    if file_time < cutoff:
                        os.remove(file_path)
                        logger.debug(f"已删除过期截图: {filename}")

    except Exception as e:
        logger.debug(f"清理旧截图时出错: {e}")


# ─────────────────────────────────────────────────────────────
# 每日签到记录持久化
# ─────────────────────────────────────────────────────────────

def save_daily_record(results):
    """
    将今日签到结果写入 stats/YYYY-MM/DD.json。
    results: run_all_accounts 返回的 final_results 列表。
    """
    try:
        today = now_local()
        month_dir = os.path.join("stats", today.strftime("%Y-%m"))
        os.makedirs(month_dir, exist_ok=True)

        day_file = os.path.join(month_dir, today.strftime("%d") + ".json")

        success_count = len([r for r in results if r and r.get('status')])
        total_count = len([r for r in results if r])
        # 取第一个账号的积分作为代表值（单账号场景）
        points = next((r.get('points') for r in results if r and r.get('points')), None)

        record = {
            "date": today.strftime("%Y-%m-%d"),
            "status": "success" if success_count == total_count and total_count > 0 else "failure",
            "success_count": success_count,
            "total_count": total_count,
            "points": points,
            "timestamp": today.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        with open(day_file, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        logger.info(f"已保存今日签到记录: {day_file}")
        return day_file
    except Exception as e:
        logger.warning(f"保存每日签到记录时出错: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# 本月签到统计
# ─────────────────────────────────────────────────────────────

def get_monthly_stats():
    """
    读取 stats/YYYY-MM/ 目录，统计本月签到情况。
    返回 dict:
      {
        'year': int, 'month': int,
        'days_in_month': int,    # 本月总天数
        'today': int,            # 今天是几号
        'passed_days': int,      # 截止今天已过天数（含今天）
        'success_days': [int],   # 签到成功的日期列表
        'failure_days': [int],   # 签到失败的日期列表
        'missing_days': [int],   # 有记录日期中缺失的（应签未签）
        'success_count': int,
        'failure_count': int,
        'missing_count': int,    # 应签但无记录天数
        'success_rate': float,   # 成功 / 已过天数
      }
    """
    today = now_local()
    year, month, day_today = today.year, today.month, today.day
    days_in_month = calendar.monthrange(year, month)[1]

    month_dir = os.path.join("stats", today.strftime("%Y-%m"))

    success_days = []
    failure_days = []
    missing_days = []  # 1..today 中没有记录的天

    for d in range(1, day_today + 1):
        day_str = f"{d:02d}"
        json_file = os.path.join(month_dir, f"{day_str}.json")
        if os.path.exists(json_file):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    rec = json.load(f)
                if rec.get("status") == "success":
                    success_days.append(d)
                else:
                    failure_days.append(d)
            except Exception:
                failure_days.append(d)
        else:
            missing_days.append(d)

    passed_days = day_today
    success_count = len(success_days)
    failure_count = len(failure_days)
    missing_count = len(missing_days)
    success_rate = (success_count / passed_days * 100) if passed_days > 0 else 0.0

    return {
        "year": year,
        "month": month,
        "days_in_month": days_in_month,
        "today": day_today,
        "passed_days": passed_days,
        "success_days": success_days,
        "failure_days": failure_days,
        "missing_days": missing_days,
        "success_count": success_count,
        "failure_count": failure_count,
        "missing_count": missing_count,
        "success_rate": success_rate,
    }


def generate_monthly_stats_html():
    """
    生成本月签到统计的 HTML 块，嵌入到每日推送报告中。
    """
    try:
        stats = get_monthly_stats()
    except Exception as e:
        logger.warning(f"生成月统计时出错: {e}")
        return ""

    year = stats["year"]
    month = stats["month"]
    days_in_month = stats["days_in_month"]
    today = stats["today"]
    passed_days = stats["passed_days"]
    success_count = stats["success_count"]
    failure_count = stats["failure_count"]
    missing_count = stats["missing_count"]
    success_rate = stats["success_rate"]
    success_days = set(stats["success_days"])
    failure_days = set(stats["failure_days"])
    missing_days = set(stats["missing_days"])

    # 评级
    if success_count == passed_days and passed_days > 0:
        grade = "🏆 全勤"
    elif success_rate >= 90:
        grade = "🥇 优秀"
    elif success_rate >= 70:
        grade = "🥈 良好"
    else:
        grade = "🥉 需加油"

    # 构建日历格子
    first_weekday = calendar.monthrange(year, month)[0]  # 0=Mon
    # 转成周日为第一列的偏移（中国习惯可选；这里用周一为首）
    cells = []
    for _ in range(first_weekday):
        cells.append('<td style="padding:2px 4px;"></td>')

    for d in range(1, days_in_month + 1):
        if d > today:
            # 未来
            style = "color:var(--text-sub,#9ca3af);"
            label = f"{d}"
        elif d in success_days:
            style = "color:#059669;font-weight:600;"
            label = f"✅{d}"
        elif d in failure_days:
            style = "color:#dc2626;font-weight:600;"
            label = f"❌{d}"
        else:
            # missing
            style = "color:#f59e0b;font-weight:600;"
            label = f"⚠️{d}"

        if d == today:
            style += "text-decoration:underline;"

        cells.append(f'<td style="padding:2px 4px;font-size:11px;text-align:center;{style}">{label}</td>')

    # 补全最后一行
    total_cells = first_weekday + days_in_month
    remainder = total_cells % 7
    if remainder != 0:
        for _ in range(7 - remainder):
            cells.append('<td style="padding:2px 4px;"></td>')

    # 拆成行
    rows_html = ""
    for i in range(0, len(cells), 7):
        rows_html += "<tr>" + "".join(cells[i:i+7]) + "</tr>"

    week_header = "<tr>" + "".join(
        f'<th style="padding:2px 4px;font-size:10px;color:var(--text-sub,#6b7280);text-align:center;font-weight:500;">{w}</th>'
        for w in ["一", "二", "三", "四", "五", "六", "日"]
    ) + "</tr>"

    # 未签天列表
    unsigned = sorted(failure_days | missing_days)
    if unsigned:
        unsigned_str = "、".join(f"{d}日" for d in unsigned if d <= today)
    else:
        unsigned_str = "无，保持全勤！🎉"

    html = f"""
    <div style="margin-top:16px;border-top:1px solid var(--border,#e5e7eb);padding-top:14px;">
        <div style="font-size:13px;font-weight:700;color:var(--text-main,#111827);margin-bottom:10px;">
            📅 {year}年{month}月签到统计
        </div>

        <!-- 数据概览 -->
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;">
            <span style="background:var(--bg-success,#ecfdf5);color:var(--text-success,#059669);
                         padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;">
                ✅ 已签 {success_count}/{passed_days} 天
            </span>
            <span style="background:{'var(--bg-error,#fef2f2)' if (failure_count+missing_count) > 0 else 'var(--bg-success,#ecfdf5)'};
                         color:{'var(--text-error,#dc2626)' if (failure_count+missing_count) > 0 else 'var(--text-success,#059669)'};
                         padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;">
                {"❌ 未签 " + str(failure_count+missing_count) + " 天" if (failure_count+missing_count) > 0 else "🎉 全勤！"}
            </span>
            <span style="background:var(--bg-footer,#f3f4f6);color:var(--text-sub,#6b7280);
                         padding:3px 10px;border-radius:12px;font-size:12px;">
                {grade} · {success_rate:.0f}%
            </span>
        </div>

        <!-- 签到日历 -->
        <table style="border-collapse:collapse;width:100%;margin-bottom:10px;">
            {week_header}
            {rows_html}
        </table>

        <!-- 未签天列表 -->
        <div style="font-size:12px;color:var(--text-sub,#6b7280);">
            <span style="font-weight:500;">未签日期：</span>{unsigned_str}
        </div>
        <div style="font-size:11px;color:var(--text-sub,#9ca3af);margin-top:4px;">
            ✅成功 ❌失败 ⚠️无记录（可能因网络问题）
        </div>
    </div>
    """
    return html


def generate_monthly_stats_markdown():
    """
    生成本月签到统计的 Markdown 文本块，用于钉钉等渠道。
    """
    try:
        stats = get_monthly_stats()
    except Exception as e:
        logger.warning(f"生成月统计 Markdown 时出错: {e}")
        return ""

    year = stats["year"]
    month = stats["month"]
    passed_days = stats["passed_days"]
    success_count = stats["success_count"]
    failure_count = stats["failure_count"]
    missing_count = stats["missing_count"]
    success_rate = stats["success_rate"]
    failure_days = set(stats["failure_days"])
    missing_days = set(stats["missing_days"])

    unsigned = sorted(failure_days | missing_days)
    unsigned_str = "、".join(f"{d}日" for d in unsigned if d <= passed_days) if unsigned else "无，全勤！🎉"

    md = f"\n---\n"
    md += f"### 📅 {year}年{month}月签到统计\n\n"
    md += f"- ✅ 已签到：**{success_count}** 天 / 已过 {passed_days} 天\n"
    md += f"- ❌ 未签到：**{failure_count + missing_count}** 天\n"
    md += f"- 📊 签到率：**{success_rate:.0f}%**\n"
    md += f"- 📋 未签日期：{unsigned_str}\n"
    return md
