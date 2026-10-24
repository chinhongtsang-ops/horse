import asyncio
import json
import os
import re
from datetime import datetime, timedelta
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "race_data.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-HK,zh;q=0.9",
}

# ==================== 1. 馬匹綜合實力分析演算法 ====================
def analyze_horse(past_runs, draw, wt, target_dist):
    # 跑法分析 (放頭 / 跟前 / 置中 / 後追)
    front, mid, back = 0, 0, 0
    for r in past_runs:
        pos_nums = re.findall(r'\d+', r.get("pos", ""))
        if pos_nums:
            first_p = int(pos_nums[0])
            if first_p <= 2:
                front += 1
            elif first_p <= 6:
                mid += 1
            else:
                back += 1

    if front >= 2 or (front == 1 and len(past_runs) == 1):
        style = "放頭/前領"
    elif mid >= 2 or (front + mid >= 2):
        style = "跟前守好位"
    elif back >= 2:
        style = "置中/後追"
    else:
        style = "中置均速"

    # 檔位分析
    draw_txt = f"{draw}檔"
    try:
        d = int(draw)
        if d <= 3:
            draw_txt = f"{d}檔 (內欄好位)"
        elif d <= 7:
            draw_txt = f"{d}檔 (中檔靈活)"
        elif d >= 10:
            draw_txt = f"{d}檔 (外檔考驗)"
    except Exception:
        pass

    # 負磅分析
    wt_txt = f"{wt}磅"
    try:
        w = int(re.search(r'\d+', str(wt)).group())
        if w >= 132:
            wt_txt = f"{w}磅 (重磅考驗)"
        elif w <= 119:
            wt_txt = f"{w}磅 (輕磅有利)"
        else:
            wt_txt = f"{w}磅 (負磅適中)"
    except Exception:
        pass

    # 場地與距離分析 (草/谷草/泥，快/好/濕)
    good_runs = [r for r in past_runs if str(r.get("place", "")).isdigit() and int(r.get("place")) <= 3]
    if good_runs:
        best = good_runs[0]
        track_apt = f"{best.get('track', '草地')} ({best.get('going', '好地')})"
        dist_apt = f"首本 {best.get('dist', target_dist)}米"
    elif past_runs:
        last = past_runs[0]
        track_apt = f"{last.get('track', '草地')} ({last.get('going', '好地')})"
        dist_apt = f"近戰 {last.get('dist', target_dist)}米"
    else:
        track_apt = "初出/待考驗"
        dist_apt = f"目標 {target_dist}"

    summary = f"跑法屬【{style}】；排{draw_txt}，負{wt_txt}；場地性能：{track_apt}，路程：{dist_apt}。"
    return {
        "style": style,
        "draw_note": draw_txt,
        "wt_note": wt_txt,
        "track_apt": track_apt,
        "dist_apt": dist_apt,
        "summary": summary
    }

# ==================== 2. aiohttp 極速抓取馬匹往績與晨課 ====================
async def fetch_html(session, url):
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                return await resp.text()
    except Exception:
        pass
    return None

async def scrape_horse_history(session, horse_id, sem):
    if not horse_id:
        return [], [], {}
    async with sem:
        h_url = f"https://racing.hkjc.com/racing/information/Chinese/Horse/Horse.aspx?HorseId={horse_id}"
        tw_url = f"https://racing.hkjc.com/racing/information/Chinese/Trackwork/TrackworkResult.aspx?HorseId={horse_id}"
        h_html, tw_html = await asyncio.gather(fetch_html(session, h_url), fetch_html(session, tw_url))

    past_runs, trackwork_list = [], []
    video_links = {
        "bt_video": f"https://racing.hkjc.com/racing/information/Chinese/Horse/BtResult.aspx?HorseId={horse_id}",
        "last_race_replay": "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx",
        "patrol_video": "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx",
    }

    if h_html:
        soup = BeautifulSoup(h_html, "html.parser")
        tables = soup.find_all("table", class_="bigborder")
        if tables:
            for row in tables[0].find_all("tr")[1:6]:
                cols = [c.get_text(strip=True) for c in row.find_all("td")]
                if len(cols) >= 14 and cols[0].isdigit():
                    replay_a = row.find("a", href=re.compile(r"ResultsAll|Replay", re.I))
                    rep_url = "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx"
                    if replay_a and replay_a.get("href"):
                        href = replay_a["href"]
                        rep_url = href if href.startswith("http") else f"https://racing.hkjc.com{href}"
                        if video_links["last_race_replay"].endswith("ResultsAll.aspx"):
                            video_links["last_race_replay"] = rep_url

                    past_runs.append({
                        "place": cols[1],
                        "date": cols[2],
                        "track": f"{cols[3]}{cols[4]}",
                        "dist": cols[5],
                        "going": cols[6],
                        "class": cols[7],
                        "draw": cols[8],
                        "rtg": cols[9],
                        "jockey": cols[11],
                        "lbw": cols[12],
                        "odds": cols[13],
                        "time": cols[15] if len(cols) > 15 else "-",
                        "pos": cols[16] if len(cols) > 16 else "-",
                        "replay_url": rep_url,
                    })
                    if len(past_runs) >= 3:
                        break

    if tw_html:
        soup_tw = BeautifulSoup(tw_html, "html.parser")
        table_tw = soup_tw.find("table", class_="table_bd")
        if table_tw:
            for tr in table_tw.find_all("tr")[1:4]:
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) >= 5:
                    trackwork_list.append({
                        "date": tds[0],
                        "type": tds[1],
                        "track": tds[2],
                        "time": tds[3],
                        "rider": tds[4],
                        "note": tds[5] if len(tds) > 5 else "",
                    })

    return past_runs, trackwork_list, video_links

# ==================== 3. 精準解析單場標題（徹底杜絕「董事盃/二級賽」誤判） ====================
def extract_accurate_race_header(soup, r_no, is_sim, r_course):
    # 刪除頁面側邊欄、導航列與廣告，只保留排位表正上方文字
    for nav in soup.find_all(["nav", "header", "footer", "select", "script", "style"]):
        nav.decompose()

    race_title = f"第 {r_no} 場賽事"
    race_cls = "越洋轉播" if is_sim else "讓賽"
    race_dist = "-"
    race_time = "-"

    # 尋找包含「第 X 場」或「米」的核心資訊區塊
    target_text = ""
    for div in soup.find_all(["div", "td", "p"]):
        txt = div.get_text(" ", strip=True)
        if ("米" in txt) and (f"第 {r_no} 場" in txt or f"第{r_no}場" in txt or "讓賽" in txt or "班" in txt):
            if 15 < len(txt) < 350:
                target_text = txt
                break

    if not target_text:
        target_text = soup.get_text(" ", strip=True)[:800]

    # 1. 班次判定 (優先匹配第一至五班，絕不誤判二級賽)
    cls_m = re.search(r'(第一班|第二班|第三班|第四班|第五班|新馬賽|公開賽|國際一級賽|國際二級賽|國際三級賽|一級賽|二級賽|三級賽|表列賽)', target_text)
    if cls_m:
        race_cls = cls_m.group(1)

    # 2. 距離判定
    dist_m = re.search(r'(\d{4})\s*米', target_text)
    if dist_m:
        race_dist = f"{dist_m.group(1)}米"

    # 3. 開跑時間
    time_m = re.search(r'(\d{1,2}:\d{2})', target_text)
    if time_m:
        race_time = time_m.group(1)

    # 4. 賽事名稱 (排除「董事盃」等非本場字眼)
    name_m = re.search(r'([^\s\d\(\)（）]{2,12}(?:讓賽|錦標|盃|賞|挑戰盃|紀念盃|大賽))', target_text)
    if name_m and "董事盃" not in name_m.group(1):
        race_title = name_m.group(1)
    else:
        race_title = f"{r_course}-{r_no} 錦標" if is_sim else f"第 {r_no} 場讓賽"

    return race_title, race_cls, race_dist, race_time

# ==================== 4. 主程序：Playwright 抓排位 + aiohttp 並發抓往績 ====================
async def main():
    print("==========================================")
    print("🏇 馬會最新排位【混合雙擎極速爬蟲】啟動中...")
    print("==========================================")

    output_data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "meetings": []
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await context.new_page()

        # 1. 打開馬會排位首頁，等待動態渲染完成
        print("[*] 正在透過瀏覽器引擎讀取馬會最新賽期選單...")
        await page.goto("https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2500)
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        meetings_map = {}

        # 從渲染後的 DOM 提取所有賽日 (包含本地 ST/HV 及海外 S1, S2, S3...)
        for match in re.finditer(r'RaceDate=(\d{4}/\d{2}/\d{2})&(?:amp;)?Racecourse=(ST|HV|S\d+)', html):
            d_str, c_str = match.group(1), match.group(2)
            meetings_map[f"{d_str}_{c_str}"] = (d_str, c_str)

        # 掃描下拉選單 <select> 內的選項 (支援各種日期格式)
        for opt in soup.find_all("option"):
            txt = opt.get_text(" ", strip=True)
            val = opt.get("value", "")
            combined = f"{val} {txt}"
            m_d = re.search(r'(\d{4}/\d{2}/\d{2})|(\d{2}/\d{2}/\d{4})', combined)
            if m_d:
                raw_d = m_d.group(0)
                fmt_d = raw_d if raw_d.startswith("20") else f"{raw_d.split('/')[2]}/{raw_d.split('/')[1]}/{raw_d.split('/')[0]}"
                if "跑馬地" in combined or "HV" in combined:
                    meetings_map[f"{fmt_d}_HV"] = (fmt_d, "HV")
                elif "沙田" in combined or "ST" in combined:
                    meetings_map[f"{fmt_d}_ST"] = (fmt_d, "ST")
                else:
                    s_m = re.search(r'\b(S\d+)\b', combined)
                    if s_m:
                        sc = s_m.group(1)
                        meetings_map[f"{fmt_d}_{sc}"] = (fmt_d, sc)

        # 從當前首頁標題直接提取正在顯示的賽期（保底必定抓到最新一期）
        page_txt = soup.get_text(" ", strip=True)
        cur_m = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', page_txt)
        if cur_m:
            fmt_d = f"{cur_m.group(1)}/{cur_m.group(2).zfill(2)}/{cur_m.group(3).zfill(2)}"
            c_code = "HV" if "跑馬地" in page_txt[:600] else "ST"
            meetings_map[f"{fmt_d}_{c_code}"] = (fmt_d, c_code)
            # 同時主動探測同日有無海外轉播 S1, S2
            for s_code in ["S1", "S2"]:
                meetings_map[f"{fmt_d}_{s_code}"] = (fmt_d, s_code)

        sorted_meetings = sorted(meetings_map.values(), key=lambda x: (x[0], x[1]))
        print(f"[✓] 候選探測賽期: {[f'{d} ({c})' for d, c in sorted_meetings]}")

        sem = asyncio.Semaphore(8)
        connector = aiohttp.TCPConnector(limit=12)
        async with aiohttp.ClientSession(connector=connector) as session:
            for r_date, r_course in sorted_meetings:
                is_sim = r_course.startswith("S") and r_course != "ST"
                v_name = "跑馬地" if r_course == "HV" else ("沙田" if r_course == "ST" else f"海外轉播 ({r_course})")

                meeting_obj = {
                    "id": f"{r_date.replace('/', '-')}_{r_course}",
                    "date": r_date,
                    "course": r_course,
                    "is_simulcast": is_sim,
                    "venue": v_name,
                    "label": f"{r_date} · {v_name}",
                    "trackInfo": f"{v_name}賽道",
                    "firstTime": "待定",
                    "race_info": [],
                    "races": {}
                }

                for r_no in range(1, 13):
                    url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx?RaceDate={r_date}&Racecourse={r_course}&RaceNo={r_no}"
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(1200)
                        r_html = await page.content()
                    except Exception:
                        break

                    r_soup = BeautifulSoup(r_html, "html.parser")
                    race_table = r_soup.find("table", id="racecardlist") or r_soup.find("table", class_="starter")
                    if not race_table:
                        break

                    # 驗證頁面日期一致（防止無海外賽事時馬會自動跳回本地賽日造成重複）
                    y, mo, da = r_date.split("/")
                    if f"{y}年{int(mo)}月{int(da)}日" not in r_soup.get_text(" ", strip=True) and r_no == 1:
                        break
                    if is_sim and r_course not in r_html and r_no == 1:
                        break

                    # 提取馬匹名單
                    parsed_horses = []
                    tbody = race_table.find("tbody") or race_table
                    for row in tbody.find_all("tr"):
                        cols = [c.get_text(strip=True) for c in row.find_all("td")]
                        if len(cols) < 6 or not cols[0].isdigit():
                            continue

                        h_link = row.find("a", href=re.compile(r"HorseId=", re.I))
                        h_name = h_link.get_text(strip=True) if h_link else (cols[3] if len(cols) > 3 and cols[3] else cols[2])
                        h_id = ""
                        if h_link:
                            hm = re.search(r"HorseId=([A-Z0-9_]+)", h_link["href"], re.I)
                            if hm:
                                h_id = hm.group(1)

                        wt = cols[5] if len(cols) > 5 else "-"
                        jky = cols[6] if len(cols) > 6 else "-"
                        dr = cols[8] if len(cols) > 8 else (cols[4] if is_sim else "-")
                        tr = cols[9] if len(cols) > 9 else (cols[7] if is_sim else "-")
                        rtg = cols[10] if len(cols) > 10 else "-"
                        gear = cols[13] if len(cols) > 13 else ""

                        parsed_horses.append({
                            "no": int(cols[0]),
                            "horse_id": h_id,
                            "name": h_name,
                            "draw": dr,
                            "wt": wt,
                            "jockey": jky,
                            "trainer": tr,
                            "rtg": rtg,
                            "gear": gear,
                        })

                    if not parsed_horses:
                        break

                    # 精準提取本場名稱、班次、距離、時間
                    race_title, race_cls, race_dist, race_time = extract_accurate_race_header(r_soup, r_no, is_sim, r_course)
                    if r_no == 1 and race_time != "-":
                        meeting_obj["firstTime"] = race_time

                    print(f"  [{v_name} R{r_no}] {race_title} ({race_cls} · {race_dist}) -> 並發抓取 {len(parsed_horses)} 匹馬往績與分析...")

                    async def enrich_horse(h):
                        past, tw, vids = await scrape_horse_history(session, h["horse_id"], sem) if (h["horse_id"] and not is_sim) else ([], [], {})
                        h["past_runs"] = past
                        h["trackwork"] = tw
                        h["videos"] = vids
                        h["analysis"] = analyze_horse(past, h["draw"], h["wt"], race_dist)
                        return h

                    horses = await asyncio.gather(*[enrich_horse(h) for h in parsed_horses])

                    btn_label = f"{r_course}-{r_no}" if is_sim else f"第 {r_no} 場"
                    meeting_obj["race_info"].append({
                        "r": r_no,
                        "btn_label": btn_label,
                        "name": race_title,
                        "time": race_time,
                        "cls": race_cls,
                        "dist": race_dist
                    })
                    meeting_obj["races"][str(r_no)] = horses

                if meeting_obj["races"]:
                    output_data["meetings"].append(meeting_obj)
                    print(f"[✓] {meeting_obj['label']} 共抓取 {len(meeting_obj['races'])} 場賽事！")

        await browser.close()

    if output_data["meetings"]:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print("\n==========================================")
        print(f"🎉 爬取大功告成！最新排位與馬匹分析已儲存至:\n👉 {DATA_FILE}")
        print("==========================================")
    else:
        print("\n[!] 暫時未發現排位資料。")

if __name__ == "__main__":
    asyncio.run(main())