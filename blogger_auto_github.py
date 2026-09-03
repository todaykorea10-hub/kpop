import os
import time
import random
import xml.etree.ElementTree as ET
import urllib.parse
import requests
import json
import re
import difflib
from bs4 import BeautifulSoup
from google import genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from googlenewsdecoder import gnewsdecoder  # pip install googlenewsdecoder

# ================= [ 설정 영역 ] =================
# [깃허브 액션용 수정] 키/ID를 코드에 직접 넣지 않고 환경변수(GitHub Secrets)에서 읽습니다.
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BLOG_ID = os.environ["BLOG_ID"]
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# 네이버/다음/구글뉴스 전체에서 K-POP 관련 화제 기사를 폭넓게 검색합니다.
# [수정] 자주 실행되는 구조상 키워드 풀이 작으면 "최근 N일" 범위 내 기사를 금방 다 써버려서
# 새 기사가 안 나오는 문제가 생깁니다. 그래서 활동 중인 그룹/아티스트와 트렌드 키워드를 대폭 늘렸습니다.
BASE_ARTIST_KEYWORDS = [
    "BTS", "블랙핑크", "트와이스", "뉴진스", "아이브", "에스파",
    "세븐틴", "스트레이키즈", "투모로우바이투게더", "엔하이픈",
    "제로베이스원", "라이즈", "르세라핌", "아이들", "케플러",
    "NCT", "레드벨벳", "샤이니", "엑소", "빅뱅", "방탄소년단",
    "장원영", "카리나", "민지", "차은우", "지민", "정국", "뷔",
]
TRENDING_KEYWORDS = [
    "컴백", "신곡", "화제", "1위", "인기", "빌보드", "월드투어",
    "콘서트", "무대", "차트", "그래미", "팬미팅", "코첼라", "MAMA",
]
ALL_KEYWORDS = BASE_ARTIST_KEYWORDS + TRENDING_KEYWORDS

# 다음뉴스, 네이버뉴스, 구글뉴스(전체) 세 곳에서 각 키워드를 검색합니다.
# 구글 뉴스의 "when:Nd" 연산자로 최근 N일 이내 기사만 검색되도록 제한합니다.
RECENCY_DAYS = 5
RECENCY_FILTER = f"when:{RECENCY_DAYS}d"

SEARCH_KEYWORDS = []
for _kw in ALL_KEYWORDS:
    SEARCH_KEYWORDS.append(f"site:news.naver.com {_kw} {RECENCY_FILTER}")   # 네이버 뉴스
    SEARCH_KEYWORDS.append(f"site:news.daum.net {_kw} {RECENCY_FILTER}")    # 다음 뉴스
    SEARCH_KEYWORDS.append(f"{_kw} {RECENCY_FILTER}")                      # 구글 뉴스 전체 (매체 제한 없음)

# 실행 1회당 최대 게시 개수
# [수정] 하루 1번만 실행되고, 그 안에서 6시간(340분, 여유를 두고 setup 등을 감안해 20분 뺌)에
# 걸쳐 40개를 천천히 나눠서 게시합니다. GitHub Actions 1회 실행 최대 시간(6시간=360분) 이내로 맞췄습니다.
MAX_POSTS_PER_RUN = 40
SPREAD_MINUTES = 340

# 이번 실행에서 게시할 글들을 이 시간(분) 안에 나눠서 올립니다 (한꺼번에 몰아 올리지 않도록)
SPREAD_MINUTES = 120

# 한 번 처리(게시했든, 중복이라 스킵했든)한 뉴스 링크를 영구히 기록해두는 파일.
POSTED_LINKS_FILE = 'posted_links.json'

# 이미 게시한 글 제목 이력 (유사도 비교용)
POSTED_TITLES_FILE = 'posted_titles.json'

# 유사도 판단 기준 (0~1, 높을수록 더 비슷해야 중복으로 판단. 0.6~0.7 권장)
SIMILARITY_THRESHOLD = 0.65

# 다음 실행까지 대기할 시간 범위 (분 단위, 이 사이에서 매번 무작위로 결정)
MIN_INTERVAL_MINUTES = 20
MAX_INTERVAL_MINUTES = 70
# =================================================

SCOPES = ['https://www.googleapis.com/auth/blogger']

def get_blogger_service():
    # [깃허브 액션용 수정] 로컬 파일 대신 환경변수(GitHub Secrets)에서 토큰을 읽습니다.
    # 브라우저 로그인 절차(run_local_server)는 서버 환경에서 쓸 수 없으므로 완전히 제거했습니다.
    token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    if not token_json_str:
        print("❌ 오류: 환경변수 GOOGLE_TOKEN_JSON이 설정되어 있지 않습니다!")
        return None

    creds = Credentials.from_authorized_user_info(json.loads(token_json_str), SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("❌ 오류: 토큰을 갱신할 수 없습니다. 로컬에서 새로 인증 후 시크릿을 갱신해주세요.")
            return None

    return build('blogger', 'v3', credentials=creds)

def load_posted_links():
    if os.path.exists(POSTED_LINKS_FILE):
        try:
            with open(POSTED_LINKS_FILE, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_posted_link(link):
    links = load_posted_links()
    links.add(link)
    with open(POSTED_LINKS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(links), f, ensure_ascii=False, indent=2)

def load_posted_titles():
    if os.path.exists(POSTED_TITLES_FILE):
        try:
            with open(POSTED_TITLES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_posted_title(title):
    titles = load_posted_titles()
    titles.append(title)
    with open(POSTED_TITLES_FILE, 'w', encoding='utf-8') as f:
        json.dump(titles, f, ensure_ascii=False, indent=2)

# [추가] 이미 게시한 제목들과 문자열 유사도를 비교해서, 비슷한 내용(중복 소재)을 걸러냅니다.
def is_similar_to_existing(title):
    existing_titles = load_posted_titles()
    for existing in existing_titles:
        ratio = difflib.SequenceMatcher(None, title.lower(), existing.lower()).ratio()
        if ratio >= SIMILARITY_THRESHOLD:
            return True
    return False

# [체크 로직] 블로그에 이미 업로드된 글 리스트를 최신순으로 분석 (제목 기반 2차 방어선)
def is_already_posted(service, title):
    try:
        posts_route = service.posts()
        result = posts_route.list(blogId=BLOG_ID, maxResults=20, status="DRAFT").execute()
        items = result.get('items', [])

        result_live = posts_route.list(blogId=BLOG_ID, maxResults=20, status="LIVE").execute()
        items += result_live.get('items', [])

        clean_target = re.sub(r'[^a-zA-Z0-9]', '', title).lower()

        for post in items:
            clean_post_title = re.sub(r'[^a-zA-Z0-9]', '', post['title']).lower()
            if clean_target in clean_post_title or clean_post_title in clean_target:
                return True
        return False
    except Exception:
        return False

# 구글 뉴스 리다이렉트는 base64로 인코딩된 값을 별도 API로 디코딩해야 실제 원문 링크가 나옵니다.
def resolve_google_news_link(google_news_link):
    try:
        result = gnewsdecoder(google_news_link, interval=1)
        if result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
        return google_news_link
    except Exception as e:
        print(f"⚠️ 원본 링크 디코딩 중 문제 발생: {e}")
        return google_news_link

# 다음뉴스 등에서 실제 기사 이미지 대신 나오는 사이트 기본 로고/플레이스홀더/광고 이미지를 걸러내기 위한 키워드
IMAGE_BLOCKLIST_KEYWORDS = [
    'logo', 'symbol', 'default', 'noimage', 'no_image', 'blank',
    'kakao_default', 'og_default', 'common/img', 'daumcdn.net/thumb/S95x64',
    'placeholder', 'icon', 'sprite', 'banner', 'button', '/ad/', 'ad_',
    'nav_', 'header_', 'footer_', 'gnb_', 'btn_',
]

def is_blocked_image(url):
    if not url:
        return True
    lowered = url.lower()
    return any(keyword in lowered for keyword in IMAGE_BLOCKLIST_KEYWORDS)

# 이미지 태그에 width/height 속성이 있는데 너무 작으면(아이콘/버튼 등) 후보에서 제외합니다.
def is_too_small(img_tag, min_size=150):
    try:
        width = img_tag.get('width')
        height = img_tag.get('height')
        if width and int(re.sub(r'\D', '', str(width)) or 0) < min_size:
            return True
        if height and int(re.sub(r'\D', '', str(height)) or 0) < min_size:
            return True
    except Exception:
        pass
    return False

def extract_news_image(news_link):
    try:
        final_link = resolve_google_news_link(news_link)
        response = requests.get(final_link, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        candidates = []

        og_image = soup.find('meta', attrs={'property': 'og:image'})
        if og_image and og_image.get('content'):
            candidates.append(og_image['content'])

        og_image_secure = soup.find('meta', attrs={'property': 'og:image:secure_url'})
        if og_image_secure and og_image_secure.get('content'):
            candidates.append(og_image_secure['content'])

        twitter_image = soup.find('meta', attrs={'name': 'twitter:image'})
        if twitter_image and twitter_image.get('content'):
            candidates.append(twitter_image['content'])

        for img in soup.find_all('img'):
            if is_too_small(img):
                continue
            src = img.get('src') or img.get('data-src')
            if src and src.startswith('http') and not src.lower().endswith(('.svg', '.gif')):
                candidates.append(src)

        # 블록리스트에 걸리지 않는 첫 번째 후보를 순서대로 채택합니다.
        for candidate in candidates:
            if not is_blocked_image(candidate):
                return candidate

        # 전부 블록리스트에 걸렸거나 후보가 아예 없는 경우 (사이트 기본 로고만 있거나, 이미지 자체가 없는 페이지)
        if candidates:
            print(f"   ↪️ 후보 이미지 {len(candidates)}개 모두 로고/아이콘으로 판단되어 제외했습니다. (예: {candidates[0][:80]})")
        else:
            print("   ↪️ 이 페이지에서는 이미지 후보 자체를 찾지 못했습니다 (연재/코너 페이지 등일 수 있음).")
        return None
    except Exception as e:
        print(f"⚠️ 이미지 추출 중 문제 발생: {e}")
        return None

# 여러 키워드로 검색한 결과를 링크 기준으로 합쳐서 더 넓은 뉴스 풀을 만듭니다.
def get_latest_news_list(keywords=None, count_per_keyword=15):
    if keywords is None:
        keywords = SEARCH_KEYWORDS

    seen_links = set()
    news_list = []

    for i, keyword in enumerate(keywords):
        print(f"🔎 Searching news list for '{keyword}'...")
        encoded_keyword = urllib.parse.quote(keyword)
        url = f"https://news.google.com/rss/search?q={encoded_keyword}&hl=ko&gl=KR&ceid=KR:ko"

        try:
            response = requests.get(url, timeout=10, headers=HEADERS)
            if response.status_code != 200:
                print(f"❌ Failed to fetch news data for '{keyword}'. (상태 코드 {response.status_code})")
                continue

            root = ET.fromstring(response.content)
            items = root.findall('.//item')
            if not items:
                print(f"   ↪️ 이 검색어는 0개 반환됨 (일시적일 수 있음)")
            for item in items[:count_per_keyword]:
                title = item.find('title').text
                link = item.find('link').text
                if link in seen_links:
                    continue
                seen_links.add(link)
                news_list.append({"title": title, "link": link})
        except Exception as e:
            print(f"⚠️ '{keyword}' 검색 중 오류: {e}")
            continue

        # 연속 요청 사이에 짧은 딜레이를 둬서 구글 뉴스의 일시적 빈 응답(soft rate-limit)을 방지합니다.
        if i < len(keywords) - 1:
            time.sleep(random.uniform(2, 4))

    return news_list

def generate_blog_content(news_title, news_link):
    print(f"🤖 Gemini is generating a long-form K-POP blog post with translation...")

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        prompt = f"""
        Korean News Title: "{news_title}"
        Source Link: {news_link}

        The Korean news above is a hot topic about K-POP, idols, and BTS. Based on this, please generate a trendy, in-depth K-POP blog post in English.

        [Requirements]
        1. **Title Translation:** Translate the "Korean News Title" into a catchy, trendy, and natural English title suitable for global K-POP fans.
        2. **Length:** The body must be a long-form article of roughly 800-1000 words (about one full A4 page). Do not write a short summary — expand with background context, fan reactions, industry context, related past activities of the artist/group, and future expectations so the article feels substantial and complete.
        3. **Structure:** Divide the article into at least 5-6 well-developed paragraphs, each with several sentences. Use a bright, energetic tone throughout. Emphasize key terms using HTML <b> tags.
        4. Create 3 to 4 catchy subtitles using HTML <h3> tags to break up the sections (e.g. background, key details, fan/industry reaction, what's next).
        5. At the very end of the post, add a source citation naturally:
           <br><br><p style="color: gray; font-size: 0.95em;">Source Article: <a href="{news_link}" target="_blank">Read the original Korean news here</a></p>
        6. **[CRITICAL]** At the very bottom of the post (after the source link), generate exactly 10 relevant and trending K-POP hashtags tailored to this specific news topic (Include #BTS if relevant). Present them neatly in a single line, separated by spaces.
        7. **[OUTPUT FORMAT]** Do NOT include any meta-comments. Output ONLY the clean JSON format structure verbatim as shown below:
        {{
          "english_title": "Translated and Catcy English Title Here",
          "english_body_html": "<h3>Subtitle</h3> <p>Long, well-developed paragraph with <b>bold</b> words...</p> ... <br><br>Source article... #Hashtags"
        }}
        """

        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
        )

        raw_text = response.text
        json_match = re.search(r'(\{.*\})', raw_text, re.DOTALL)
        if json_match:
            json_text = json_match.group(1).strip()
            result = json.loads(json_text)
            return result.get("english_title"), result.get("english_body_html")
        else:
            print("❌ 제미나이가 JSON 형식을 반환하지 않았습니다.")
            return None, None

    except Exception as e:
        print(f"❌ Gemini API Error: {e}")
        return None, None

def extract_hashtags_as_labels(content_html, max_labels=10):
    tags = re.findall(r'#(\w+)', content_html)
    seen = set()
    labels = []
    for tag in tags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            labels.append(tag)
        if len(labels) >= max_labels:
            break
    return labels

def sanitize_title(title, max_len=100):
    if not title:
        return title
    replacements = {
        '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
        '\u2026': '...',
    }
    for old, new in replacements.items():
        title = title.replace(old, new)
    title = re.sub(r'[\x00-\x1f\x7f]', '', title)
    title = title.strip()
    if len(title) > max_len:
        title = title[:max_len].rstrip() + "..."
    return title

def post_to_blogger(service, english_title, content, image_url, max_retries=3):
    english_title = sanitize_title(english_title)
    full_title = f"[K-POP] {english_title}"
    print("🚀 Uploading to Google Blogger with Real News Image & Translated Title...")

    if image_url:
        # referrerpolicy="no-referrer": 블로그(blogspot.com)에서 요청했다는 정보를 원본 사이트에
        # 보내지 않도록 해서, 리퍼러 기반 핫링크 차단(많은 한국 뉴스 사이트가 사용)을 우회합니다.
        image_html = f"""<div style="text-align: center; margin-top: 10px; margin-bottom: 25px;">
            <img src="{image_url}" alt="K-POP Real News Scene" referrerpolicy="no-referrer" loading="lazy" style="max-width: 100%; height: auto; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);" />
        </div><br>"""
    else:
        image_html = ""

    final_content = image_html + content
    labels = extract_hashtags_as_labels(content)
    labels = [l[:190] for l in labels][:20]
    if labels:
        print(f"   🏷️ 라벨 지정: {', '.join(labels)}")

    body = {
        "kind": "blogger#post",
        "title": full_title,
        "content": final_content,
        "status": "LIVE",
        "labels": labels
    }

    for attempt in range(1, max_retries + 1):
        try:
            posts = service.posts()
            request = posts.insert(blogId=BLOG_ID, body=body)
            response = request.execute()
            print(f"✅ Successfully posted to Blogger! (Link: {response.get('url')})")
            return True
        except Exception as e:
            error_text = str(e)
            if "429" in error_text or "rateLimitExceeded" in error_text or "Resource has been exhausted" in error_text:
                wait_seconds = 30 * attempt
                print(f"⏳ Blogger API 쿼터 한도(429)에 걸렸습니다. {wait_seconds}초 후 재시도합니다. (시도 {attempt}/{max_retries})")
                time.sleep(wait_seconds)
                continue
            print("❌ Blogger Upload Error (전체 내용):")
            print(error_text)
            return False

    print("❌ 최대 재시도 횟수를 초과했습니다. 이번 글은 게시를 포기합니다.")
    return False

def run_job():
    print(f"\n{'='*60}")
    print(f"⏰ 자동 실행 시작: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    service = get_blogger_service()
    if not service:
        return

    posted_links = load_posted_links()
    news_items = get_latest_news_list()
    print(f"📋 검색된 후보 기사: 총 {len(news_items)}개")

    success_count = 0
    skipped_count = 0
    similar_skip_count = 0

    for index, news in enumerate(news_items, 1):
        if success_count >= MAX_POSTS_PER_RUN:
            print(f"\n🎯 이번 실행의 최대 게시 개수({MAX_POSTS_PER_RUN}개)에 도달했습니다.")
            break

        # 1차 방어선: 이미 처리(게시/중복확인)했던 링크는 API 호출 없이 바로 스킵
        if news['link'] in posted_links:
            skipped_count += 1
            continue

        refined_title = news['title'].split(" - ")[0]
        print(f"\n[기사 후보 {index}] 분석 중: {refined_title}")

        english_title, blog_html = generate_blog_content(refined_title, news['link'])

        if english_title and blog_html:
            # 2차 방어선: 번역된 제목이 블로그에 이미 존재하는지 확인 (완전/부분 일치)
            if is_already_posted(service, english_title):
                print(f"   ⚠️ 중복 확인됨: '{english_title}'은 이미 게시된 글이므로 패스합니다.")
                save_posted_link(news['link'])
                continue

            # 3차 방어선: 이전에 게시한 제목들과 내용이 비슷한지 확인 (다른 매체의 같은 소식 등)
            if is_similar_to_existing(english_title):
                print(f"   ⚠️ 유사한 내용의 기존 글이 있어 건너뜁니다: '{english_title}'")
                save_posted_link(news['link'])
                similar_skip_count += 1
                continue

            real_image_url = extract_news_image(news['link'])
            if real_image_url:
                print(f"   🖼️ 이미지 추출 성공: {real_image_url}")
            else:
                print(f"   ⚠️ 이미지 추출 실패: 이미지 없이 게시됩니다.")

            success = post_to_blogger(service, english_title, blog_html, real_image_url)

            if success:
                save_posted_link(news['link'])
                save_posted_title(english_title)
                success_count += 1
                print(f"   📊 이번 실행 누적 게시: {success_count}/{MAX_POSTS_PER_RUN}")

                # [수정] 하루 1회 실행 안에서 6시간에 걸쳐 천천히 나눠 게시하도록,
                # 목표 개수에 아직 도달하지 않았고 더 게시할 후보가 남아있다면 다음 게시까지 대기합니다.
                if success_count < MAX_POSTS_PER_RUN:
                    base_interval = SPREAD_MINUTES / MAX_POSTS_PER_RUN
                    wait_minutes = random.uniform(base_interval * 0.7, base_interval * 1.3)
                    next_post_time = time.strftime('%H:%M:%S', time.localtime(time.time() + wait_minutes * 60))
                    print(f"   ⏳ 다음 게시까지 약 {wait_minutes:.1f}분 대기합니다. (예상 시각: {next_post_time})")
                    time.sleep(wait_minutes * 60)
        else:
            # 생성 자체가 실패한 링크도 다음에 또 시도하지 않도록 기록
            save_posted_link(news['link'])

    print(f"\n🏁 이번 실행에서 총 {success_count}개 글을 게시했습니다.")
    print(f"   (전체 후보 {len(news_items)}개 중 이미 처리됨 {skipped_count}개, 유사/중복 제외 {similar_skip_count}개)")

if __name__ == "__main__":
    # [깃허브 액션용 수정] 무한 루프 대신 1회만 실행하고 종료합니다.
    # 반복 실행은 .github/workflows/kpop-auto.yml의 cron 스케줄이 담당합니다.
    run_job()
