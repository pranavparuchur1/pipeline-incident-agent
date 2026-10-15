with orders as (
    select
        customer_id,
        min(ordered_at) as first_order_at,
        count(*) as order_count,
        sum(case when status in ('placed', 'shipped') then order_amount else 0 end) as lifetime_value
    from {{ ref('stg_orders') }}
    group by customer_id
)

select
    c.customer_id,
    c.email,
    c.first_name,
    c.country,
    c.signup_date,
    o.first_order_at,
    coalesce(o.order_count, 0) as order_count,
    coalesce(o.lifetime_value, 0) as lifetime_value
from {{ ref('stg_customers') }} as c
left join orders as o on o.customer_id = c.customer_id
