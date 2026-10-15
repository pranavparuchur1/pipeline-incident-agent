select
    partition_date,
    channel,
    count(*) as orders,
    count(*) filter (where status = 'cancelled') as cancelled_orders,
    coalesce(sum(order_amount) filter (where status in ('placed', 'shipped')), 0) as revenue
from {{ ref('stg_orders') }}
group by partition_date, channel
