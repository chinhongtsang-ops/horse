import asyncio
from datetime import datetime
import json
import os
import re
import aiohttp
from bs4 import BeautifulSoup
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "race_data.json")
HTML_FILE = os.path.join(BASE_DIR, "index.html")

app = FastAPI(title="馬場實戰筆記 Pro")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

scraper_state = {
    "is_running": False,
    "progress": "閒置中",
    "last_updated": None
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-HK,zh;q=0.9",
}

async def fetch_html(session, url):
  try:
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=8)) as resp:
      if resp.status == 200:
        return await resp.text()
  except:
    pass
  return None

def analyze_horse(past_runs, draw, wt):
  style = "跟前守好位"
  front = sum(1 for r in past_runs if r.get("pos", "").startswith(("1-", "2-")))
  back = sum(1 for r in past_runs if any(r.get("pos", "").startswith(f"{i}-") for i in range(7, 15)))
  if front >= 2: style = "前領/放頭"
  elif back >= 2: style = "置中留後追"

  draw_txt = f"{draw}檔"
  try:
    d = int(draw)
    if d <= 3: draw_txt += " (內欄慳位)"
    elif d >= 10: draw_txt += " (外疊考驗)"
  except: pass

  wt_txt = f"{wt}磅"
  try:
    w = int(re.search(r'\d+', str(wt)).group())
    if w >= 132: wt_txt += " (負頂磅)"
    elif w <= 118: wt_txt += " (輕磅受惠)"
  except: pass

  return {
      "style": style,
      "draw_note": draw_txt,
      "wt_note": wt_txt,
      "track_apt": "草地好地",
      "summary": f"跑法【{style}】。{draw_txt}，{wt_txt}。"
  }

async def scrape_horse_history(session, horse_id):
  if not horse_id:
    return [], [], {}
  h_url = f"https://racing.hkjc.com/racing/information/Chinese/Horse/Horse.aspx?HorseId={horse_id}"
  tw_url = f"https://racing.hkjc.com/racing/information/Chinese/Trackwork/TrackworkResult.aspx?HorseId={horse_id}"
  h_html, tw_html = await asyncio.gather(fetch_html(session, h_url), fetch_html(session, tw_url))

  past_runs, trackwork_list = [], []
  video_links = {
      "bt_video": f"https://racing.hkjc.com/racing/information/Chinese/Horse/BtResult.aspx?HorseId={horse_id}",
      "last_race_replay": "",
      "patrol_video": "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx",
  }

  if h_html:
    soup = BeautifulSoup(h_html, "html.parser")
    tables = soup.find_all("table", class_="bigborder")
    if tables:
      for row in tables[0].find_all("tr")[1:5]:
        cols = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cols) >= 14 and cols[0].isdigit():
          past_runs.append({
              "place": cols[1],
              "date": cols[2],
              "track": f"{cols[3]} {cols[4]}",
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
              "replay_url": "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx",
          })

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

async def run_scraper_task():
  global scraper_state
  if scraper_state["is_running"]: return
  scraper_state["is_running"] = True
  scraper_state["progress"] = "正在探測馬會最新排位賽期..."

  output_data = {
      "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
      "meetings": []
  }

  try:
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
      # 1. 探測馬會當前首頁（直接取得官方最新排位資訊）
      base_url = "https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx"
      base_html = await fetch_html(session, base_url)
      if not base_html:
        raise Exception("無法讀取馬會排位首頁")

      # 抓出最新賽日的日期及場地
      meetings_to_scrape = []
      # 由首頁 URL 或頁面文字抓出真實最新日期
      date_match = re.search(r'RaceDate=(\d{4}/\d{2}/\d{2})&(?:amp;)?Racecourse=([A-Z0-9]+)', base_html)
      if date_match:
        d_str, c_str = date_match.group(1), date_match.group(2)
        v_name = "跑馬地" if c_str == "HV" else ("沙田" if c_str == "ST" else f"越洋賽事 ({c_str})")
        meetings_to_scrape.append({"date": d_str, "course": c_str, "venue": v_name, "is_sim": c_str.startswith("S") and c_str != "ST"})

      # 掃描頁面內其他所有可選賽日 (包含海外轉播 S1/S2)
      for m in re.finditer(r'RaceDate=(\d{4}/\d{2}/\d{2})&(?:amp;)?Racecourse=([A-Z0-9]+)', base_html):
        d_str, c_str = m.group(1), m.group(2)
        key = f"{d_str}_{c_str}"
        if key not in [f"{x['date']}_{x['course']}" for x in meetings_to_scrape]:
          is_sim = c_str.startswith("S") and c_str != "ST"
          v_name = "跑馬地" if c_str == "HV" else ("沙田" if c_str == "ST" else f"越洋賽事 ({c_str})")
          meetings_to_scrape.append({"date": d_str, "course": c_str, "venue": v_name, "is_sim": is_sim})

      for m in meetings_to_scrape:
        r_date, r_course = m["date"], m["course"]
        is_sim = m["is_sim"]
        scraper_state["progress"] = f"正在抓取 {r_date} {m['venue']} 排位..."

        meeting_obj = {
            "id": f"{r_date.replace('/', '-')}_{r_course}",
            "date": r_date,
            "course": r_course,
            "is_simulcast": is_sim,
            "venue": m["venue"],
            "label": f"{r_date} · {m['venue']}",
            "trackInfo": f"{m['venue']}草地",
            "firstTime": "待定",
            "race_info": [],
            "races": {}
        }

        for r_no in range(1, 13):
          url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx?RaceDate={r_date}&Racecourse={r_course}&RaceNo={r_no}"
          html = await fetch_html(session, url)
          if not html or ("racecardlist" not in html and "starter" not in html):
            break

          soup = BeautifulSoup(html, "html.parser")
          
          # 精準定位賽事資訊列（徹底避開側邊欄董事盃廣告）
          race_table = soup.find("table", id="racecardlist") or soup.find("table", class_="starter")
          if not race_table:
            break

          # 提取真實賽事標題與班次
          race_title = f"第 {r_no} 場賽事"
          race_cls = "讓賽"
          race_dist = "-"
          race_time = "-"

          # 鎖定排位表上方正中央的 race_info 區塊
          header_div = soup.find("div", class_="race_info") or soup.find("div", class_="f_fl race_title")
          h_text = header_div.get_text(" ", strip=True) if header_div else ""

          cls_m = re.search(r'(第一班|第二班|第三班|第四班|第五班|新馬賽|國際[一二三]級賽|[一二三]級賽|公開賽)', h_text)
          if cls_m: race_cls = cls_m.group(1)

          dist_m = re.search(r'(\d{4})米', h_text)
          if dist_m: race_dist = f"{dist_m.group(1)}米"

          time_m = re.search(r'(\d{1,2}:\d{2})', h_text)
          if time_m: race_time = time_m.group(1)

          title_m = re.search(r'([^\s\d]{2,12}(?:讓賽|錦標|盃|賞|紀念盃|挑戰盃|大賽))', h_text)
          if title_m:
            race_title = title_m.group(1)
          else:
            race_title = f"第 {r_no} 場 ({race_cls})"

          if r_no == 1 and race_time != "-":
            meeting_obj["firstTime"] = race_time

          # 提取馬匹列表
          parsed_horses = []
          tbody = race_table.find("tbody") or race_table
          for row in tbody.find_all("tr"):
            cols = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cols) < 8 or not cols[0].isdigit():
              continue

            h_link = row.find("a", href=re.compile(r"HorseId=", re.I))
            h_name = h_link.get_text(strip=True) if h_link else cols[3]
            h_id = ""
            if h_link:
              hm = re.search(r"HorseId=([A-Z0-9_]+)", h_link["href"], re.I)
              if hm: h_id = hm.group(1)

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

          # 非同步補充近績
          async def enrich(h):
            past, tw, vids = await scrape_horse_history(session, h["horse_id"])
            analysis = analyze_horse(past, h["draw"], h["wt"])
            h["past_runs"] = past
            h["trackwork"] = tw
            h["videos"] = vids
            h["analysis"] = analysis
            return h

          horses = await asyncio.gather(*[enrich(h) for h in parsed_horses])

          if horses:
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

      # 儲存到本地檔案
      if output_data["meetings"]:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
          json.dump(output_data, f, ensure_ascii=False, indent=2)
        scraper_state["progress"] = "排位更新完成！"
      else:
        scraper_state["progress"] = "未能獲取最新排位"

  except Exception as e:
    scraper_state["progress"] = f"錯誤: {str(e)}"
  finally:
    scraper_state["is_running"] = False

# ==================== 路由 ====================
@app.get("/")
async def serve_index(): return FileResponse(HTML_FILE)

@app.get("/api/data")
async def get_race_data():
  if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r", encoding="utf-8") as f:
      return JSONResponse(content=json.load(f))
  return JSONResponse(content={"updated_at": None, "meetings": []})

@app.post("/api/scrape")
async def trigger_scrape(background_tasks: BackgroundTasks):
  if scraper_state["is_running"]: return {"status": "running", "progress": scraper_state["progress"]}
  background_tasks.add_task(run_scraper_task)
  return {"status": "started", "progress": "正在抓取最新排位..."}

@app.get("/api/scrape/status")
async def get_scrape_status(): return scraper_state

if __name__ == "__main__":
  uvicorn.run(app, host="0.0.0.0", port=8000)
