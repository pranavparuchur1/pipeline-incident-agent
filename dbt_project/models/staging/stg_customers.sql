select
    customer_id,
    email,
    first_name,
    country,
    signup_date,
    _dt as partition_date,
    _source_file,
    _run_id
from {{ source('raw', 'customers') }}
