-- =====================================================================
-- Hermes · 0004_measurement_views.sql · views that feed the decision gates
-- security_invoker = true so every view obeys the caller's row-level security.
-- =====================================================================
begin;

-- Gate 1.1 / 2.1 (AI share only; add non-AI variable items in the workbook)
create view app.v_ai_cost_30d with (security_invoker = true) as
select c.customer_id,
       sum(c.cost_usd)                         as ai_cost_usd_30d,
       sum(c.tokens_in)::bigint                as tokens_in_30d,
       sum(c.tokens_out)::bigint               as tokens_out_30d,
       count(*) filter (where c.outcome <> 'success') as non_success_calls
from app.agent_calls c
where c.customer_id is not null and c.ts > now() - interval '30 days'
group by c.customer_id;

-- Gate 1.6: share of inquiries by source
create view app.v_inquiry_sources_30d with (security_invoker = true) as
select i.source, count(*) as inquiries,
       round(count(*)::numeric / nullif(sum(count(*)) over (), 0), 4) as share
from app.inquiries i
where i.received_at > now() - interval '30 days'
group by i.source;

-- Gates 1.5 / 2.3: recurring human minutes per customer per month (onboarding excluded)
create view app.v_human_minutes_monthly with (security_invoker = true) as
select date_trunc('month', t.occurred_at)::date as month, t.customer_id,
       sum(t.minutes) filter (where t.activity <> 'onboarding') as recurring_minutes,
       sum(t.minutes) filter (where t.activity = 'onboarding')  as onboarding_minutes
from app.time_entries t
where t.customer_id is not null
group by 1, 2;

-- Gate 1.4: owner approval latency (days)
create view app.v_approval_latency with (security_invoker = true) as
select a.customer_id,
       percentile_cont(0.5) within group (order by extract(epoch from a.decided_at - a.requested_at) / 86400.0) as median_days,
       count(*) filter (where a.decision = 'expired') as expired
from app.approvals a
where a.decided_at is not null or a.decision = 'expired'
group by a.customer_id;

-- Gate 3.5: concentration of collections by payment channel
create view app.v_collection_channels with (security_invoker = true) as
select p.channel, sum(p.amount) as amount,
       round(sum(p.amount) / nullif(sum(sum(p.amount)) over (), 0), 4) as share
from app.payments p where p.status = 'matched'
group by p.channel;

grant select on app.v_ai_cost_30d, app.v_inquiry_sources_30d, app.v_human_minutes_monthly,
               app.v_approval_latency, app.v_collection_channels to authenticated;
commit;
