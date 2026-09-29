import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import sys
import time
import re
import configparser

LINKS_FILE = "links.txt"   # общий файл для хранения всех найденных ссылок

VK_DOMAINS = ('vk.com', 'vk.ru', 'vkvideo.ru', 'vk.cc', 'm.vk.com')

# Глобальные переменные для VK API (заполняются в main)
VK_API_TOKEN = None
VK_API_VERSION = "5.199"


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
    Секции:
      [smtp]     — EMAIL, SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD (обязательно)
      [vk]       — api_token, api_version (необязательно; значения по умолчанию для VK-источников)
      [sourceN]  — url (обязательно), filters (необязательно),
                   type (необязательно: 'vk' или 'web'; иначе — автоопределение),
                   api_token (необязательно), api_version (необязательно),
                   count (необязательно; количество загружаемых постов, по умолчанию 100)
    """
    config = configparser.ConfigParser()
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
    """
    Получает ID сообщества ВКонтакте по его короткому имени (domain).
    Возвращает положительный ID (например, 1598048) или None.
    """
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
    """
    Получает ссылки на посты сообщества через VK API (метод wall.get).
    Для каждого поста генерируется ссылка вида https://vk.com/wall{owner_id}_{id}.
    Дополнительно собираются ссылки из текста и вложений типа 'link',
    но только те, что принадлежат этому же сообществу (если задан group_id).
    """
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
            # Генерируем ссылку на сам пост
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


def get_vk_post_text(post_url, api_token, api_version="5.199"):
    """
    Получает текст поста ВКонтакте по ссылке вида https://vk.ru/wall-..._...
    Возвращает очищенный текст (обрезанный до 200 символов) или None.
    """
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
        "v": api_version
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

    text = post.get("text", "")
    if not text:
        print(f"Пост {posts} не содержит текста.")
        return None

    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > 200:
        text = text[:200] + "..."
    return text


def filter_links(links, patterns):
    """
    Применяет regex-фильтры. Ссылка остаётся, если соответствует хотя бы одному паттерну.
    Если patterns пуст — возвращает все ссылки без изменений.
    """
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


def send_email(recipient, subject, body_html,
               smtp_server, smtp_port, smtp_user, smtp_password):
    """Отправляет HTML-письмо через SMTP с TLS."""
    msg = MIMEMultipart("alternative")
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html", "utf-8"))

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

    all_new_links = set()
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

        if src_type == 'vk' and api_token:
            domain = url.rstrip('/').split('/')[-1]
            if domain.startswith('@'):
                domain = domain[1:]

            group_id = get_vk_group_id(domain, api_token, api_version)
            if group_id:
                print(f"ID сообщества {domain}: {group_id}")
            else:
                print(f"Не удалось получить ID сообщества {domain}, "
                      f"ссылки не будут отфильтрованы по сообществу.")

            print(f"Используем VK API для сообщества: {domain} "
                  f"(v{api_version}, count={count})")
            page_links = fetch_links_from_vk_api(domain, api_token, api_version,
                                                 count=count, group_id=group_id)
        else:
            if src_type == 'vk' and not api_token:
                print("Для VK-источника не задан api_token, используем HTML-парсинг.")
            page_links = fetch_links_from_page(url)

        if not page_links:
            print(f"Не удалось получить ссылки с {url}, пропускаем.")
            continue

        print(f"Получено ссылок: {len(page_links)}")
        filtered_links = filter_links(page_links, filters)
        print(f"После фильтрации: {len(filtered_links)}")
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
        if is_vk_post_url(link) and VK_API_TOKEN:
            post_text = get_vk_post_text(link, VK_API_TOKEN, VK_API_VERSION)
            if post_text:
                subject = f"Новая ссылка: {post_text}"
            else:
                title = get_page_title(link)
                subject = f"Новая ссылка: {title}" if title else f"Новая ссылка: {link}"
        else:
            title = get_page_title(link)
            subject = f"Новая ссылка: {title}" if title else f"Новая ссылка: {link}"

        safe_link = (link.replace('&', '&amp;')
                         .replace('<', '&lt;')
                         .replace('>', '&gt;')
                         .replace('"', '&quot;'))
        body_html = f"""
        <html>
        <body>
            <p>Обнаружена новая ссылка:</p>
            <p><a href="{safe_link}">{safe_link}</a></p>
        </body>
        </html>
        """
        send_email(recipient, subject, body_html,
                   smtp_server, smtp_port, smtp_user, smtp_password)
        time.sleep(1)


if __name__ == "__main__":
    main()