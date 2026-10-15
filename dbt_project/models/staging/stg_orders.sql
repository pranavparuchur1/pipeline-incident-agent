select
    order_id,
    customer_id,
    order_ts as ordered_at,
    status,
    channel,
    currency,
    order_total as order_amount,
    _dt as partition_date,
    _source_file,
    _run_id
from {{ source('raw', 'orders') }}
