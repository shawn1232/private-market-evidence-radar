from pathlib import Path
import argparse
import os
import stat

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ROOT / "sessions"
SESSIONS.mkdir(parents=True, exist_ok=True)

PLATFORMS = {
    "xiaohongshu": {
        "label": "小红书",
        "url": "https://www.xiaohongshu.com/explore",
        "hint": "登录后可抓公开笔记、账号页和关键词搜索页。",
    },
    "zsxq": {
        "label": "知识星球",
        "url": "https://www.zsxq.com/",
        "hint": "登录后优先保留原帖或专栏页 URL，再回到工作台抓取。",
    },
    "weixin": {
        "label": "微信公众号后台",
        "url": "https://mp.weixin.qq.com/",
        "hint": "适合核实公众号原文、菜单和小程序相关入口。",
    },
    "wechat_open": {
        "label": "微信开放平台",
        "url": "https://open.weixin.qq.com/",
        "hint": "适合核实小程序、开放能力和生态合作入口。",
    },
    "yuque": {
        "label": "语雀",
        "url": "https://www.yuque.com/",
        "hint": "适合抓知识库、文档页和行业资料库。",
    },
    "feishu": {
        "label": "飞书",
        "url": "https://www.feishu.cn/",
        "hint": "适合抓飞书文档、知识库和公开资料页。",
    },
}


def confirm_save(label: str, hint: str) -> bool:
    """Use a visible Windows dialog so login works from the hidden one-click launcher."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return bool(
                messagebox.askokcancel(
                    f"保存{label}登录态",
                    f"{hint}\n\n请先在浏览器中完成登录，然后点击“确定”保存。\n如果不想保存，请点击“取消”。",
                    parent=root,
                )
            )
        finally:
            root.destroy()
    except Exception:
        try:
            input(f"请在浏览器里完成“{label}”登录，然后回到这个窗口按回车保存登录态：")
            return True
        except EOFError:
            return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("--url", default=None)
    args = parser.parse_args()

    meta = PLATFORMS[args.platform]
    url = args.url or meta["url"]
    state_path = SESSIONS / f"{args.platform}.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        print(f"[提示] {meta['hint']}")
        if not confirm_save(meta["label"], meta["hint"]):
            browser.close()
            raise SystemExit("用户取消，本次没有保存登录态。")
        context.storage_state(path=str(state_path))
        try:
            os.chmod(state_path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
        browser.close()

    print(f"[OK] {meta['label']} 登录态已保存：{state_path}")


if __name__ == "__main__":
    main()
