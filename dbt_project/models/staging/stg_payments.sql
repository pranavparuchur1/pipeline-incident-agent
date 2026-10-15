select
    payment_id,
    order_id,
    method,
    amount,
    paid_ts as paid_at,
    _dt as partition_date,
    _source_file,
    _run_id
from {{ source('raw', 'payments') }}
