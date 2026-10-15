select
    order_item_id,
    order_id,
    product_id,
    quantity,
    unit_price,
    quantity * unit_price as line_amount,
    _dt as partition_date,
    _source_file,
    _run_id
from {{ source('raw', 'order_items') }}
