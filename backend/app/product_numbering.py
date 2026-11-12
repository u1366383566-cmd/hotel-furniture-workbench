"""Stable product numbers and names shared by exports and future delivery."""
from datetime import datetime

SLOT_ORDER = {'white_isolated': '01', 'twin_bed': '02', 'nightstand_closeup': '03', 'panorama': '04'}


def format_product_code(number):
    if not isinstance(number, int) or number < 1:
        raise ValueError('无效的产品编号')
    return f'TFMK{number:03d}'


def product_image_name(number, archived_at, slot_type):
    try:
        date = datetime.strptime(archived_at, '%Y-%m-%d %H:%M:%S')
        order = SLOT_ORDER[slot_type]
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError('归档时间或四图类型无效') from exc
    return f'{format_product_code(number)}-X{date.year % 10}{date.month}-{order}-ST.png'


def allocate_product_number(conn):
    row = conn.execute('SELECT ready, last_number FROM product_number_state WHERE id=1').fetchone()
    if not row or not row['ready']:
        raise ValueError('历史产品编号尚未确认完成')
    number = row['last_number'] + 1
    conn.execute('UPDATE product_number_state SET last_number=? WHERE id=1', (number,))
    return number
