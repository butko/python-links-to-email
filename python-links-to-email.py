import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import os
import sys
import time
import re
import html
import configparser

LINKS_FILE = "links.txt"   # общий файл для хранения всех найденных ссылок

VK_DOMAINS = ('vk.com', 'vk.ru', 'vkvideo.ru', 'vk.cc', 'm.vk.com')

# Глобальные переменные для VK API (заполняются в main)
VK_API_TOKEN = None
VK_API_VERSION = "5.199"

# Параметры вложений (значения по умолчанию, могут быть переопределены из конфига)
ATTACH_PHOTOS = True
PHOTO_TARGET_WIDTH = 600     # желаемая ширина фото (px)
PHOTO_MAX_COUNT = 10         # максимум фото в одном письме
PHOTO_MAX_KB = 500           # максимум размера одного фото (КБ)


def detect_source_type(url):
    """Определяет тип источника по домену URL: 'vk' или 'web'."""
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        for d in VK_DOMAINS:
            if host == d or host.endswith('.' + d):
                return 'vk'
        return 'web'
    except Exception:
        return 'web'


def is_vk_post_url(url):
    """Проверяет, является ли URL ссылкой на пост ВКонтакте (содержит /wall)."""
    return bool(re.search(r'wall(-?\d+)_(\d+)', url))


def read_config(config_file="config.txt"):
    """
    Читает конфигурационный файл в формате INI.
    Интерполяция '%' отключена, чтобы URL с закодированными символами
    (например, %2C) читались без ошибок.
    """
    global ATTACH_PHOTOS, PHOTO_TARGET_WIDTH, PHOTO_MAX_COUNT, PHOTO_MAX_KB

    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(config_file, encoding='utf-8')
    except Exception as e:
        print(f"Ошибка чтения {config_file}: {e}")
        sys.exit(1)

    if not config.has_section('smtp'):
        print("Ошибка: в конфиге отсутствует секция [smtp]")
        sys.exit(1)

    smtp_params = {}
    for key in ["EMAIL", "SMTP_SERVER", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"]:
        if config.has_option('smtp', key):
            smtp_params[key] = config.get('smtp', key)
        else:
            print(f"Ошибка: в секции [smtp] отсутствует параметр {key}")
            sys.exit(1)

    try:
        smtp_params["SMTP_PORT"] = int(smtp_params["SMTP_PORT"])
    except ValueError:
        print("Ошибка: SMTP_PORT должен быть числом.")
        sys.exit(1)

    # Дополнительные параметры вложений (необязательные)
    attach_str = config.get('smtp', 'ATTACH_PHOTOS', fallback='yes').strip().lower()
    ATTACH_PHOTOS = attach_str in ('yes', 'true', '1', 'on')

    try:
        PHOTO_TARGET_WIDTH = int(config.get('smtp', 'PHOTO_TARGET_WIDTH', fallback='600'))
    except ValueError:
        PHOTO_TARGET_WIDTH = 600

    try:
        PHOTO_MAX_COUNT = int(config.get('smtp', 'PHOTO_MAX_COUNT', fallback='10'))
    except ValueError:
        PHOTO_MAX_COUNT = 10

    try:
        PHOTO_MAX_KB = int(config.get('smtp', 'PHOTO_MAX_KB', fallback='500'))
    except ValueError:
        PHOTO_MAX_KB = 500

    # Общие настройки VK
    vk_default_token = None
    vk_default_version = "5.199"
    if config.has_section('vk'):
        vk_default_token = config.get('vk', 'api_token', fallback=None)
        vk_default_version = config.get('vk', 'api_version', fallback="5.199")

    # Источники
    sources = []
    for section in config.sections():
        if section in ('smtp', 'vk'):
            continue
        if not config.has_option(section, 'url'):
            print(f"Предупреждение: в секции {section} отсутствует 'url', пропускаем.")
            continue

        url = config.get(section, 'url').strip()
        filters_str = config.get(section, 'filters', fallback='').strip()
        filters = [f.strip() for f in filters_str.split(',') if f.strip()] if filters_str else []

        source = {'url': url, 'filters': filters}

        # Тип источника
        explicit_type = config.get(section, 'type', fallback='').strip().lower()
        if explicit_type in ('vk', 'web'):
            source['type'] = explicit_type
        else:
            source['type'] = detect_source_type(url)

        # Токен только для VK
        if source['type'] == 'vk':
            if config.has_option(section, 'api_token'):
                source['api_token'] = config.get(section, 'api_token').strip()
            elif vk_default_token:
                source['api_token'] = vk_default_token
            else:
                print(f"Предупреждение: для VK-источника {section} не задан api_token, "
                      f"будет использован HTML-парсинг.")

        source['api_version'] = config.get(section, 'api_version',
                                           fallback=vk_default_version)

        # Количество загружаемых постов (для VK)
        try:
            source['count'] = int(config.get(section, 'count', fallback='100'))
        except ValueError:
            source['count'] = 100
        sources.append(source)

    if not sources:
        print("Ошибка: не найдено ни одной секции с параметром 'url'.")
        sys.exit(1)

    return smtp_params, sources, vk_default_token, vk_default_version


def fetch_links_from_page(url):
    """Загружает страницу и возвращает множество абсолютных URL из тегов <a>."""
    try:
        response = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; LinkMonitor/1.0)'
        })
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Ошибка при загрузке страницы {url}: {e}")
        return set()

    soup = BeautifulSoup(response.text, "html.parser")
    links = set()
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        absolute_url = urljoin(url, href)
        links.add(absolute_url)
    return links


def get_vk_group_id(domain, api_token, api_version="5.199"):
    """Получает ID сообщества ВКонтакте по короткому имени (domain)."""
    url = "https://api.vk.com/method/groups.getById"
    params = {
        "group_id": domain,
        "access_token": api_token,
        "v": api_version
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        if "error" in data:
            err = data["error"]
            print(f"Ошибка VK API (groups.getById) для {domain}: "
                  f"{err.get('error_msg', 'неизвестная ошибка')} "
                  f"(code={err.get('error_code')})")
            return None

        resp = data.get("response")
        if isinstance(resp, list) and resp:
            return resp[0].get("id")
        elif isinstance(resp, dict):
            groups = resp.get("groups")
            if isinstance(groups, list) and groups:
                return groups[0].get("id")
            elif "id" in resp:
                return resp.get("id")
    except Exception as e:
        print(f"Ошибка при получении ID сообщества {domain}: {e}")
    return None


def fetch_links_from_vk_api(domain, api_token, api_version="5.199",
                            count=100, group_id=None):
    """Получает ссылки на посты сообщества через VK API (метод wall.get)."""
    url = "https://api.vk.com/method/wall.get"
    params = {
        "domain": domain,
        "count": count,
        "access_token": api_token,
        "v": api_version
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"Ошибка при запросе к VK API для {domain}: {e}")
        return set()

    if "error" in data:
        err = data["error"]
        print(f"Ошибка VK API для {domain}: {err.get('error_msg', 'неизвестная ошибка')}")
        return set()

    links = set()
    items = data.get("response", {}).get("items", [])
    print(f"VK API вернул постов: {len(items)}")

    for post in items:
        owner_id = post.get("owner_id")
        post_id = post.get("id")
        if owner_id is not None and post_id is not None:
            post_link = f"https://vk.com/wall{owner_id}_{post_id}"
            links.add(post_link)

        # Ссылки из текста поста (с фильтром по group_id)
        for u in re.findall(r'https?://[^\s|\[\]]+', post.get("text", "")):
            u = u.rstrip('.,;:!?)')
            if group_id:
                if f'wall-{group_id}_' in u:
                    links.add(u)
            else:
                links.add(u)

        # Ссылки из вложений типа link (с фильтром по group_id)
        for att in post.get("attachments", []):
            if att.get("type") == "link":
                link_url = att.get("link", {}).get("url")
                if link_url:
                    if group_id:
                        if f'wall-{group_id}_' in link_url:
                            links.add(link_url)
                    else:
                        links.add(link_url)

    return links


def _pick_photo_size(sizes, target_width=600):
    """
    Выбирает размер фотографии, ближайший к target_width, но не больше
    target_width * 1.5. Если все размеры больше — берёт самый маленький.
    """
    valid = [s for s in sizes if s.get('width') and s.get('url')]
    if not valid:
        return None
    max_allowed = int(target_width * 1.5)
    valid.sort(key=lambda s: s['width'])
    candidates = [s for s in valid if s['width'] <= max_allowed]
    if candidates:
        return min(candidates, key=lambda s: abs(s['width'] - target_width))
    return valid[0]


def get_vk_post_details(post_url, api_token, api_version="5.199",
                        photo_target_width=600):
    """Получает детальную информацию о посте ВКонтакте."""
    match = re.search(r'wall(-?\d+)_(\d+)', post_url)
    if not match:
        print(f"Не удалось извлечь owner_id/post_id из URL: {post_url}")
        return None
    owner_id = match.group(1)
    post_id = match.group(2)
    posts = f"{owner_id}_{post_id}"

    url = "https://api.vk.com/method/wall.getById"
    params = {
        "posts": posts,
        "access_token": api_token,
        "v": api_version,
        "copy_history_depth": 2
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
    except Exception as e:
        print(f"Ошибка сети при запросе wall.getById для {posts}: {e}")
        return None

    if "error" in data:
        err = data["error"]
        print(f"Ошибка VK API (wall.getById): "
              f"{err.get('error_msg', 'неизвестная ошибка')} "
              f"(code={err.get('error_code')})")
        return None

    resp = data.get("response")
    post = None
    if isinstance(resp, list) and resp:
        post = resp[0]
    elif isinstance(resp, dict):
        items = resp.get("items")
        if isinstance(items, list) and items:
            post = items[0]
        elif "text" in resp:
            post = resp

    if not post:
        print(f"VK API не вернул пост для {posts}. Ответ: {data}")
        return None

    details = {
        'text': post.get("text", "").strip(),
        'copy_history': [],
        'photos': [],
        'videos': [],
        'docs': [],
        'links': [],
        'other': []
    }

    for ch in post.get("copy_history", []) or []:
        ch_text = ch.get("text", "").strip()
        if ch_text:
            details['copy_history'].append(ch_text)
        _process_vk_attachments(ch.get("attachments", []) or [], details,
                                photo_target_width)

    _process_vk_attachments(post.get("attachments", []) or [], details,
                            photo_target_width)

    return details


def _process_vk_attachments(attachments, details, photo_target_width=600):
    """Разбирает вложения VK и наполняет details."""
    for att in attachments:
        att_type = att.get("type")
        if att_type == "photo":
            photo = att.get("photo", {})
            sizes = photo.get("sizes", [])
            chosen = _pick_photo_size(sizes, photo_target_width)
            if chosen:
                details['photos'].append({
                    'url': chosen['url'],
                    'width': chosen.get('width'),
                    'height': chosen.get('height')
                })
        elif att_type == "video":
            video = att.get("video", {})
            title = video.get("title")
            if title:
                details['videos'].append(title)
        elif att_type == "doc":
            doc = att.get("doc", {})
            title = doc.get("title")
            if title:
                details['docs'].append(title)
        elif att_type == "link":
            link = att.get("link", {})
            title = link.get("title") or link.get("url")
            url = link.get("url")
            if url:
                details['links'].append((title, url))
        elif att_type == "wall":
            wall = att.get("wall", {})
            wall_text = wall.get("text", "").strip()
            if wall_text:
                details['copy_history'].append(wall_text)
            _process_vk_attachments(wall.get("attachments", []) or [], details,
                                    photo_target_width)
        else:
            if att_type:
                details['other'].append(att_type)


def filter_links(links, patterns):
    """Применяет regex-фильтры. Ссылка остаётся, если соответствует хотя бы одному паттерну."""
    if not patterns:
        return links
    filtered = set()
    for link in links:
        for pat in patterns:
            try:
                if re.search(pat, link):
                    filtered.add(link)
                    break
            except re.error as e:
                print(f"Некорректный regex '{pat}': {e}")
    return filtered


def get_page_title(page_url):
    """Загружает страницу и возвращает заголовок <title> или None."""
    try:
        response = requests.get(page_url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; LinkMonitor/1.0)'
        })
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            return title_tag.string.strip()
    except Exception as e:
        print(f"Не удалось получить заголовок для {page_url}: {e}")
    return None


def read_existing_links(filename):
    """Читает файл со списком ссылок и возвращает множество."""
    if not os.path.exists(filename):
        return set()
    with open(filename, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def write_new_links(filename, new_links):
    """Добавляет новые ссылки В НАЧАЛО файла, сохраняя уже существующие ниже."""
    if not new_links:
        return

    existing = []
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            existing = [line.rstrip('\n') for line in f if line.strip()]

    with open(filename, "w", encoding="utf-8") as f:
        for link in new_links:
            f.write(link + "\n")
        for line in existing:
            f.write(line + "\n")

    print(f"Добавлено {len(new_links)} новых ссылок в начало {filename}")


def download_photos(photos, target_width, max_count, max_kb):
    """Скачивает фотографии. Возвращает список вложений и словарь url->cid."""
    attachments = []
    url_to_cid = {}
    max_bytes = max_kb * 1024

    for idx, photo in enumerate(photos[:max_count], 1):
        url = photo['url']
        try:
            resp = requests.get(url, timeout=15, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; LinkMonitor/1.0)'
            })
            resp.raise_for_status()
            content = resp.content
            size_kb = len(content) // 1024
            if len(content) > max_bytes:
                print(f"Фото #{idx} ({size_kb} КБ) превышает лимит {max_kb} КБ, пропускаем")
                continue

            mimetype = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
            if not mimetype.startswith('image/'):
                mimetype = 'image/jpeg'
            ext = mimetype.split('/')[-1]
            if ext == 'jpeg':
                ext = 'jpg'
            filename = f"photo_{idx}.{ext}"
            cid = f"vkphoto{idx}"

            attachments.append((filename, content, mimetype, cid))
            url_to_cid[url] = cid
            print(f"Скачано фото #{idx}: {size_kb} КБ, {mimetype}")
        except Exception as e:
            print(f"Не удалось скачать фото #{idx}: {e}")

    return attachments, url_to_cid


def send_email(recipient, subject, body_html,
               smtp_server, smtp_port, smtp_user, smtp_password,
               attachments=None):
    """Отправляет HTML-письмо через SMTP с TLS. attachments — список (filename, content, mimetype, cid)."""
    if attachments:
        msg = MIMEMultipart("related")
    else:
        msg = MIMEMultipart("alternative")

    msg["From"] = smtp_user
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    for att in attachments or []:
        try:
            filename, content, mimetype, cid = att
            maintype, subtype = mimetype.split('/', 1)
            part = MIMEBase(maintype, subtype)
            part.set_payload(content)
            encoders.encode_base64(part)
            if cid:
                part.add_header('Content-ID', f'<{cid}>')
                part.add_header('Content-Disposition', 'inline', filename=filename)
            else:
                part.add_header('Content-Disposition', 'attachment', filename=filename)
            msg.attach(part)
        except Exception as e:
            print(f"Ошибка при прикреплении файла {att[0]}: {e}")

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        print(f"Письмо отправлено на {recipient} с темой: {subject}")
    except Exception as e:
        print(f"Ошибка при отправке письма на {recipient}: {e}")


def main():
    global VK_API_TOKEN, VK_API_VERSION

    smtp_params, sources, vk_default_token, vk_default_version = read_config()
    recipient = smtp_params["EMAIL"]
    smtp_server = smtp_params["SMTP_SERVER"]
    smtp_port = smtp_params["SMTP_PORT"]
    smtp_user = smtp_params["SMTP_USER"]
    smtp_password = smtp_params["SMTP_PASSWORD"]

    VK_API_TOKEN = vk_default_token
    VK_API_VERSION = vk_default_version

    print(f"Email получателя: {recipient}")
    print(f"SMTP сервер: {smtp_server}:{smtp_port}")
    print(f"Найдено источников: {len(sources)}")
    print(f"Вложения фото: {'вкл' if ATTACH_PHOTOS else 'выкл'}, "
          f"целевая ширина: {PHOTO_TARGET_WIDTH} px, "
          f"макс. фото: {PHOTO_MAX_COUNT}, макс. размер: {PHOTO_MAX_KB} КБ")

    all_new_links = set()
    link_sources = {}

    for src in sources:
        url = src['url']
        filters = src['filters']
        src_type = src.get('type', 'web')
        api_token = src.get('api_token')
        api_version = src.get('api_version', '5.199')
        count = src.get('count', 100)

        print(f"\nОбрабатываем URL: {url}")
        print(f"Тип источника: {src_type}")
        print(f"Фильтры: {filters if filters else 'не заданы (все ссылки)'}")

        parsed = urlparse(url)
        source_domain = parsed.netloc.lower()
        if source_domain.startswith('www.'):
            source_domain = source_domain[4:]

        if src_type == 'vk' and api_token:
            domain = url.rstrip('/').split('/')[-1]
            if domain.startswith('@'):
                domain = domain[1:]

            group_id = get_vk_group_id(domain, api_token, api_version)
            if group_id:
                print(f"ID сообщества {domain}: {group_id}")
                source_label = f"{source_domain}/club{group_id}"
            else:
                print(f"Не удалось получить ID сообщества {domain}.")
                source_label = source_domain

            print(f"Используем VK API для сообщества: {domain} "
                  f"(v{api_version}, count={count})")
            page_links = fetch_links_from_vk_api(domain, api_token, api_version,
                                                 count=count, group_id=group_id)
        else:
            if src_type == 'vk' and not api_token:
                print("Для VK-источника не задан api_token, используем HTML-парсинг.")
            page_links = fetch_links_from_page(url)
            source_label = source_domain

        if not page_links:
            print(f"Не удалось получить ссылки с {url}, пропускаем.")
            continue

        print(f"Получено ссылок: {len(page_links)}")
        filtered_links = filter_links(page_links, filters)
        print(f"После фильтрации: {len(filtered_links)}")

        for link in filtered_links:
            link_sources[link] = source_label
        all_new_links.update(filtered_links)

    if not all_new_links:
        print("\nНе найдено подходящих ссылок ни с одного источника. Завершение.")
        return

    existing_links = read_existing_links(LINKS_FILE)
    print(f"\nВ файле {LINKS_FILE} уже есть ссылок: {len(existing_links)}")

    new_links = all_new_links - existing_links
    print(f"Новых (ещё не сохранённых) ссылок: {len(new_links)}")

    if not new_links:
        print("Нет новых ссылок. Завершение.")
        return

    write_new_links(LINKS_FILE, new_links)

    for link in new_links:
        source_label = link_sources.get(link, '')
        attachments = []

        if is_vk_post_url(link) and VK_API_TOKEN:
            details = get_vk_post_details(link, VK_API_TOKEN, VK_API_VERSION,
                                          photo_target_width=PHOTO_TARGET_WIDTH)
            if details:
                # Формируем тему
                text_for_subject = details['text']
                if not text_for_subject and details['copy_history']:
                    text_for_subject = details['copy_history'][0]
                if text_for_subject:
                    subject_text = text_for_subject[:200]
                    if len(text_for_subject) > 200:
                        subject_text += "..."
                else:
                    parts = []
                    if details['photos']:
                        parts.append(f"Фото: {len(details['photos'])}")
                    if details['videos']:
                        parts.append(f"Видео: {len(details['videos'])}")
                    if details['docs']:
                        parts.append(f"Документы: {len(details['docs'])}")
                    if details['links']:
                        parts.append(f"Ссылки: {len(details['links'])}")
                    if details['other']:
                        parts.append(f"Прочее: {len(details['other'])}")
                    subject_text = ", ".join(parts) if parts else "Пост без текста"

                base_subject = f"Новая ссылка: {subject_text}"

                # Скачиваем фото (если включено)
                url_to_cid = {}
                if ATTACH_PHOTOS and details['photos']:
                    attachments, url_to_cid = download_photos(
                        details['photos'],
                        target_width=PHOTO_TARGET_WIDTH,
                        max_count=PHOTO_MAX_COUNT,
                        max_kb=PHOTO_MAX_KB
                    )

                # Формируем HTML
                body_parts = [
                    '<p>Обнаружена новая ссылка:</p>',
                    f'<p><a href="{html.escape(link, quote=True)}">'
                    f'{html.escape(link)}</a></p>'
                ]

                content_html = ['<div style="margin-top: 15px; padding: 10px; '
                                'border-left: 3px solid #4a90d9; background: #f9f9f9;">']

                if details['text']:
                    content_html.append(
                        f'<p><strong>Текст поста:</strong><br>'
                        f'{html.escape(details["text"]).replace(chr(10), "<br>")}</p>'
                    )
                for ch_text in details['copy_history']:
                    content_html.append(
                        f'<p><strong>Репост:</strong><br>'
                        f'{html.escape(ch_text).replace(chr(10), "<br>")}</p>'
                    )

                if details['photos']:
                    photos_block = ['<p><strong>Фото:</strong></p>']
                    for photo in details['photos']:
                        u = photo['url']
                        w = photo.get('width')
                        h = photo.get('height')
                        dim = f" ({w}×{h})" if w and h else ""
                        if u in url_to_cid:
                            cid = url_to_cid[u]
                            photos_block.append(
                                f'<p><img src="cid:{cid}" '
                                f'style="max-width: 100%; max-height: 600px;" '
                                f'alt="Фото{dim}"></p>'
                            )
                        else:
                            photos_block.append(
                                f'<p><a href="{html.escape(u, quote=True)}">'
                                f'{html.escape(u)}</a>{dim}</p>'
                            )
                    content_html.append("".join(photos_block))

                if details['videos']:
                    videos_text = ", ".join(html.escape(v) for v in details['videos'])
                    content_html.append(f'<p><strong>Видео:</strong> {videos_text}</p>')

                if details['docs']:
                    docs_text = ", ".join(html.escape(d) for d in details['docs'])
                    content_html.append(f'<p><strong>Документы:</strong> {docs_text}</p>')

                if details['links']:
                    links_text = ", ".join(
                        f'<a href="{html.escape(u, quote=True)}">{html.escape(t)}</a>'
                        for t, u in details['links']
                    )
                    content_html.append(f'<p><strong>Ссылки:</strong> {links_text}</p>')

                if details['other']:
                    other_text = ", ".join(html.escape(o) for o in details['other'])
                    content_html.append(f'<p><strong>Прочее:</strong> {other_text}</p>')

                content_html.append('</div>')
                body_parts.append("".join(content_html))
                body_html = "<html><body>" + "".join(body_parts) + "</body></html>"
            else:
                title = get_page_title(link)
                subject_text = title if title else link
                base_subject = f"Новая ссылка: {subject_text}"
                safe_link = html.escape(link, quote=True)
                body_html = f"""
                <html>
                <body>
                    <p>Обнаружена новая ссылка:</p>
                    <p><a href="{safe_link}">{safe_link}</a></p>
                </body>
                </html>
                """
        else:
            title = get_page_title(link)
            subject_text = title if title else link
            base_subject = f"Новая ссылка: {subject_text}"
            safe_link = html.escape(link, quote=True)
            body_html = f"""
            <html>
            <body>
                <p>Обнаружена новая ссылка:</p>
                <p><a href="{safe_link}">{safe_link}</a></p>
            </body>
            </html>
            """

        if source_label:
            subject = f"Новая ссылка ({source_label}): " + base_subject[len("Новая ссылка: "):]
        else:
            subject = base_subject

        send_email(recipient, subject, body_html,
                   smtp_server, smtp_port, smtp_user, smtp_password,
                   attachments=attachments)
        time.sleep(1)


if __name__ == "__main__":
    main()