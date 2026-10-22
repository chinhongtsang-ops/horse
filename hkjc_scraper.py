import asyncio
import json
import re
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# 設定你想爬嘅賽馬日同場地 (HV = 跑馬地, ST = 沙田)
RACE_DATE = "2026/10/19"
RACE_COURSE = "ST"
TOTAL_RACES = 11


async def scrape_horse_details(context, horse_id):
  """進入馬匹專頁，抓取：1.近4仗往績  2.最新晨課紀錄  3.試閘/賽事影片連結"""
  page = await context.new_page()
  horse_url = f"https://racing.hkjc.com/racing/information/Chinese/Horse/Horse.aspx?HorseId={horse_id}"

  past_runs = []
  trackwork_list = []
  video_links = {
      "last_race_replay": "",
      "bt_video": (
          "https://racing.hkjc.com/racing/information/Chinese/Horse/BtResult.aspx"
      ),  # 預設試閘查詢頁
      "profile_url": horse_url,
  }

  try:
    await page.goto(horse_url, wait_until="domcontentloaded", timeout=20000)
    await page.wait_for_timeout(1500)  # 等待動態表格渲染
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    # --- 1. 解析往績表格 (近4仗) ---
    # 尋找包含「場次」「名次」「日期」嘅往績 Table
    tables = soup.find_all("table", class_="bigborder")
    if tables:
      rows = tables[0].find_all("tr")
      for row in rows[1:]:
        cols = [c.get_text(strip=True) for c in row.find_all("td")]
        # 過濾分隔線或標題行 (通常有效往績行會有超過 12 欄)
        if len(cols) >= 14 and cols[0].isdigit():
          race_date_str = cols[2]  # 格式如 07/10/26
          rc_track_course = f"{cols[3]} {cols[4]}"  # 如 跑馬地 草地 "A"
          dist = cols[5]
          going = cols[6]
          cls = cols[7]
          draw = cols[8]
          rtg = cols[9]
          jockey = cols[11]
          lbw = cols[12]
          odds = cols[13]
          finish_time = cols[15] if len(cols) > 15 else "-"
          running_pos = cols[16] if len(cols) > 16 else "-"

          # 提取馬會賽事重溫直連 (如果該行有 replay link)
          replay_a = row.find("a", href=re.compile(r"ResultsAll|Replay", re.I))
          replay_url = ""
          if replay_a and replay_a.get("href"):
            href = replay_a["href"]
            replay_url = (
                href
                if href.startswith("http")
                else f"https://racing.hkjc.com{href}"
            )
            if not video_links["last_race_replay"]:
              video_links["last_race_replay"] = replay_url

          past_runs.append({
              "place": cols[1],
              "date": race_date_str,
              "track": rc_track_course,
              "dist": dist,
              "going": going,
              "class": cls,
              "draw": draw,
              "rtg": rtg,
              "jockey": jockey,
              "lbw": lbw,
              "odds": odds,
              "time": finish_time,
              "pos": running_pos,
              "replay_url": replay_url,
          })
          if len(past_runs) >= 4:
            break

    # --- 2. 抓取晨課操練紀錄 (Trackwork) ---
    # 馬會馬匹頁或操練頁 URL
    tw_url = f"https://racing.hkjc.com/racing/information/Chinese/Trackwork/TrackworkResult.aspx?HorseId={horse_id}"
    await page.goto(tw_url, wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(1000)
    tw_soup = BeautifulSoup(await page.content(), "html.parser")
    tw_table = tw_soup.find("table", class_="table_bd")
    if tw_table:
      for tr in tw_table.find_all("tr")[1:4]:  # 取最新 3 課操練
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) >= 5:
          trackwork_list.append({
              "date": tds[0],
              "type": tds[1],  # 快操 / 踱步 / 試閘
              "track": tds[2],  # 沙田全天候 / 草地
              "time": tds[3],  # 分段時間 如 24.5 (51.2)
              "rider": tds[4],  # 策騎者
              "gear": tds[5] if len(tds) > 5 else "-",
          })

    # 組裝專屬試閘結果與試閘片連結
    video_links["bt_video"] = (
        f"https://racing.hkjc.com/racing/information/Chinese/Horse/BtResult.aspx?HorseId={horse_id}"
    )

  except Exception as e:
    print(f"  [!] 抓取馬匹 {horse_id} 詳細資料時發生錯誤: {e}")
  finally:
    await page.close()

  return past_runs, trackwork_list, video_links


async def scrape_racecard(
    race_date=RACE_DATE, race_course=RACE_COURSE, max_races=TOTAL_RACES
):
  all_races_data = {
      "date": race_date,
      "venue": "跑馬地" if race_course == "HV" else "沙田",
      "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
      "races": {},
  }

  async with async_playwright() as p:
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = await context.new_page()

    for r_no in range(1, max_races + 1):
      url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx?RaceDate={race_date}&Racecourse={race_course}&RaceNo={r_no}"
      print(f"[*] 正在爬取第 {r_no} 場排位表: {url}")

      try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2000)
        soup = BeautifulSoup(await page.content(), "html.parser")

        # 尋找排位表主 Table
        race_table = soup.find("table", id="racecardlist") or soup.find(
            "table", class_="starter"
        )
        horses = []

        if race_table:
          rows = race_table.find("tbody").find_all("tr")
          for row in rows:
            cols = row.find_all("td")
            if len(cols) < 10:
              continue

            horse_no = cols[0].get_text(strip=True)
            if not horse_no.isdigit():
              continue

            # 提取馬名與 HorseId
            horse_link = row.find("a", href=re.compile(r"HorseId="))
            horse_name = (
                horse_link.get_text(strip=True)
                if horse_link
                else cols[3].get_text(strip=True)
            )
            horse_id = ""
            if horse_link:
              m = re.search(r"HorseId=([A-Z0-9_]+)", horse_link["href"])
              if m:
                horse_id = m.group(1)

            weight = cols[5].get_text(strip=True)
            jockey = cols[6].get_text(strip=True)
            draw = cols[8].get_text(strip=True)
            trainer = cols[9].get_text(strip=True)
            rating = cols[10].get_text(strip=True) if len(cols) > 10 else "-"
            gear = cols[13].get_text(strip=True) if len(cols) > 13 else ""

            print(
                f"  -> 抓取馬匹 #{horse_no} {horse_name} ({horse_id})"
                " 往績與晨課..."
            )
            past_runs, trackwork, videos = (
                await scrape_horse_details(context, horse_id)
                if horse_id
                else ([], [], {})
            )

            horses.append({
                "no": int(horse_no),
                "horse_id": horse_id,
                "name": horse_name,
                "draw": draw,
                "wt": weight,
                "jockey": jockey,
                "trainer": trainer,
                "rtg": rating,
                "gear": gear,
                "past_runs": past_runs,
                "trackwork": trackwork,
                "videos": videos,
            })

        all_races_data["races"][str(r_no)] = horses

      except Exception as e:
        print(f"[!] 第 {r_no} 場爬取失敗: {e}")

    await browser.close()

  # 儲存為 JSON 供前端讀取
  with open("race_data.json", "w", encoding="utf-8") as f:
    json.dump(all_races_data, f, ensure_ascii=False, indent=2)
  print("\n[✓] 爬取完成！已輸出至 race_data.json")


if __name__ == "__main__":
  asyncio.run(scrape_racecard())