select
    product_id,
    sku,
    name,
    category,
    unit_price,
    active,
    _source_file,
    _run_id
from {{ source('raw', 'products') }}
