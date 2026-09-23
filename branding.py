"""Local EKT brand assets and presentation, separate from purchasing calculations."""

from base64 import b64encode
from functools import lru_cache
from pathlib import Path
import re

import streamlit as st


ASSETS = Path(__file__).resolve().parent / 'assets'


def brand_html(markup):
    # Streamlit sanitizes inline SVG. Local image data URLs retain the artwork.
    def svg_image(match):
        svg = match.group().replace('currentColor', '#2C7294')
        if 'xmlns=' not in svg:
            svg = svg.replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
        payload = b64encode(svg.encode('utf-8')).decode('ascii')
        return f'<img src="data:image/svg+xml;base64,{payload}" alt="" aria-hidden="true">'
    st.html(re.sub(r'<svg\b.*?</svg>', svg_image, markup, flags=re.S))


@lru_cache(maxsize=1)
def brand_css():
    fonts = []
    for weight, name in [(400, 'regular'), (700, 'bold')]:
        payload = b64encode((ASSETS / f'pt-sans-{name}.ttf').read_bytes()).decode('ascii')
        fonts.append(
            "@font-face {font-family:'PT Sans';font-style:normal;"
            f"font-weight:{weight};font-display:swap;"
            f"src:url(data:font/ttf;base64,{payload}) format('truetype');}}"
        )
    return '\n'.join(fonts) + (ASSETS / 'app.css').read_text(encoding='utf-8')


def icon(name, size=24):
    paths = {
        'box': '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="M3 8v9l9 5 9-5V8M12 13v9M7.5 5.5l9 5"/>',
        'chart': '<path d="M4 3v17h17M8 14l4-5 4 3 5-7"/>',
        'calendar': '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 11h18M8 15h2M14 15h2"/>',
        'truck': '<path d="M3 6h11v12H3zM14 10h4l3 4v4h-7"/><circle cx="7" cy="18" r="2"/><circle cx="17" cy="18" r="2"/>',
        'growth': '<path d="m3 17 7-7 4 4 7-10M15 4h6v6"/>',
        'shield': '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    }
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{paths[name]}</svg>')


def render_brand(page_name='План закупок'):
    st.html(f'<style>{brand_css()}</style>')
    logo = b64encode((ASSETS / 'ekt-logo.svg').read_bytes()).decode('ascii')
    is_bi = page_name == 'BI-аналитика'
    title = 'BI-аналитика' if is_bi else 'План закупок'
    subtitle = ('Продажи, история расчётов и проверка точности прогнозов.' if is_bi else
                'От истории продаж — к обоснованному заказу поставщику.')
    navigation = (f'<a class="ekt-nav-active" href="#purchase-plan">{icon("chart", 19)} Аналитика закупок</a>' if is_bi else
                  f'<a class="ekt-nav-active" href="#purchase-plan">{icon("box", 19)} Заказы поставщикам</a>'
                  '<a href="#input-data">Данные и параметры</a><a href="#methodology">Как рассчитываем</a>')
    brand_html(f'''
    <div class="ekt-topbar">
      <span>Группа компаний «Электрокомплект»</span>
      <div><span>Кабинет отдела закупа</span><span class="ekt-language">РУС</span></div>
    </div>
    <header class="ekt-masthead">
      <a class="ekt-brand" href="https://ekt.kz/" target="_blank" rel="noopener noreferrer" aria-label="Сайт Электрокомплект">
        <img src="data:image/svg+xml;base64,{logo}" alt="Группа компаний Электрокомплект" width="220" height="47">
      </a>
      <div class="ekt-app-name"><strong>Управление закупками</strong><span>Планирование пополнения склада</span></div>
      <a class="ekt-site-link" href="https://ekt.kz/" target="_blank" rel="noopener noreferrer">На сайт компании ↗</a>
    </header>
    <nav class="ekt-nav" aria-label="Разделы страницы">
      {navigation}
    </nav>
    <div class="ekt-page-heading" id="purchase-plan">
      <div class="ekt-breadcrumb">Рабочее пространство <span>/</span> Закупки</div>
      <div class="ekt-heading-row"><div><h1>{title}</h1>
      <p>{subtitle}</p></div>
      <span class="ekt-local-status"><i></i> Данные на вашем компьютере</span></div>
    </div>
    ''')


def render_empty():
    brand_html('''
    <section class="ekt-empty" aria-label="Начало работы">
      <span class="ekt-eyebrow">ЗАКАЗЫ ПОСТАВЩИКАМ</span>
      <div class="ekt-order-art" aria-hidden="true">
        <svg width="180" height="145" viewBox="0 0 180 145" fill="none">
          <circle cx="90" cy="74" r="66" fill="#EEF4F7"/>
          <rect x="46" y="17" width="91" height="112" rx="5" fill="white" stroke="#2C7294" stroke-width="2"/>
          <rect x="65" y="10" width="52" height="15" rx="3" fill="#2C7294"/>
          <path d="M64 47h54M81 68h37M81 87h37M81 106h26" stroke="#C5D6DF" stroke-width="3" stroke-linecap="round"/>
          <path d="m62 66 4 4 7-8m-11 23 4 4 7-8m-11 23 4 4 7-8" stroke="#2C7294" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
          <circle cx="141" cy="105" r="22" fill="#F4B301"/>
          <path d="M131 105h20m-8-8 8 8-8 8" stroke="#0B4366" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
      <h2>Сформируйте первый заказ</h2>
      <p>Загрузите архивы поставщиков и проверьте параметры слева.<br>
      Рассчитаем потребность и объясним количество по каждой позиции.</p>
      <div class="ekt-supplier-tags"><span><i></i> IEK</span><span><i></i> Systeme Electric</span></div>
      <div class="ekt-steps">
        <div><b>01</b><span>Загрузите данные</span></div>
        <div><b>02</b><span>Проверьте расчёт</span></div>
        <div><b>03</b><span>Скачайте заказ</span></div>
      </div>
    </section>
    ''')


def render_methodology():
    features = [
        ('chart', 'История продаж', 'Регулярный спрос без разовых всплесков'),
        ('calendar', 'Сезонность', 'Месячные колебания спроса'),
        ('box', 'Остатки', 'Запас, доступный для продажи'),
        ('truck', 'Товары в пути', 'Поставки с известной датой прибытия'),
        ('growth', 'Рост спроса', 'Тренд и заданный прирост'),
        ('shield', 'Упущенный спрос', 'Корректировка по журналу stockout'),
    ]
    cards = ''.join(f'<div class="ekt-feature"><span>{icon(symbol)}</span>'
                    f'<div><h3>{title}</h3><p>{description}</p></div></div>'
                    for symbol, title, description in features)
    brand_html(f'<section class="ekt-methodology" id="methodology"><h2>Что учитывает расчёт</h2>'
            f'<div class="ekt-feature-grid">{cards}</div></section>')
    with st.expander('Формула расчёта и ограничения данных'):
        st.markdown('**Заказ = прогноз + страховой запас − остаток − поставки, которые прибудут вовремя.** '
                    'Количество округляется вверх до кратности поставщика.')
        st.write('Разовые крупные продажи ограничиваются по размеру документа. '
                 'В исходных архивах нет ID клиента. Компенсация упущенного спроса '
                 'включается после загрузки журнала отсутствия товара.')
        st.write('Остаток IEK относится к началу сентября 2026 года. Дату свободного остатка '
                 'Systeme Electric нужно подтвердить. Перед рабочим заказом загрузите актуальные '
                 'остатки и проверьте сроки поставки. Заказы поставщикам автоматически не отправляются.')


def render_footer():
    st.html('<footer class="ekt-footer"><span>Электрокомплект · Планирование закупок</span>'
            '<span>Заказ проверяет и утверждает ответственный сотрудник</span></footer>')
