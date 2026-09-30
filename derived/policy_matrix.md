# Policy matrix (generated — do not edit)

A = allow · D = deny · P = proposal (queued for a human who executes it) · blank = not permitted

| action | site_builder | content | replies | competitor | search | billing | quality | triage |
|---|---|---|---|---|---|---|---|---|
| `assets:write_draft` |  | A |  |  |  |  |  |  |
| `auth:*` |  |  |  | D |  |  |  |  |
| `billing:*` | D | D | D | D | D |  | D | D |
| `content:draft` |  | A |  |  |  |  |  |  |
| `content:edit` |  |  |  |  |  |  | D |  |
| `content:image_generate` |  | A |  |  |  |  |  |  |
| `content:publish` |  | P |  |  |  |  | D |  |
| `content:read` |  |  |  |  |  |  | A |  |
| `contract:read` |  |  |  |  |  | A |  |  |
| `customer:delete` | D |  | D |  |  | D | D | D |
| `customer:read_private` |  | D |  | D | D |  |  |  |
| `flag:raise` |  |  |  |  |  |  | A |  |
| `git:commit` | A |  |  |  |  |  |  |  |
| `guard:check` |  |  |  |  |  |  | A |  |
| `invoice:mark_draft` |  |  |  |  |  | A |  |  |
| `invoice:mark_paid` |  |  |  |  |  | P |  |  |
| `kb:read` |  |  | A |  |  |  | A |  |
| `lead:draft` |  |  |  |  | A |  |  |  |
| `lead:export` |  |  |  |  | P |  |  |  |
| `lead:verify` |  |  |  |  | A |  |  |  |
| `message:send` |  |  |  |  | P |  |  |  |
| `meta:publish` |  |  | D | D |  |  |  |  |
| `payment:*` | D | D | D | D | D |  | D | D |
| `payment:discount` |  |  |  |  |  | D |  |  |
| `payment:refund` |  |  |  |  |  | D |  |  |
| `payment:transfer` |  |  |  |  |  | D |  |  |
| `pii:read` |  |  |  |  | D |  |  |  |
| `price:quote` |  |  | D |  |  |  |  |  |
| `public:read` |  |  |  | A | A |  |  |  |
| `queue:classify` |  |  |  |  |  |  |  | A |
| `receipt:read` |  |  |  |  |  | A |  |  |
| `reply:draft` |  |  | A |  |  |  |  | D |
| `reply:send` |  |  | P |  |  |  | D | D |
| `report:draft` |  |  |  | A |  |  |  |  |
| `secrets:read` | D |  |  |  |  | D | D |  |
| `site:build` | A |  |  |  |  |  |  |  |
| `site:deploy_prod` | P | D |  |  |  |  |  |  |
| `site:deploy_staging` | A |  |  |  |  |  |  |  |
| `site:read` | A |  |  |  |  |  |  |  |
| `subscription:activate` |  |  |  |  |  | P |  |  |

| agent | enabled | budget class | per call | per task | class cap |
|---|---|---|---|---|---|
| agent_site_builder | yes | onboarding | 0.3 | 2.4 | 2.4 |
| agent_content | yes | monthly_active | 0.08 | 0.4 | 0.4 |
| agent_replies | yes | monthly_active | 0.02 | 0.05 | 0.2 |
| agent_competitor | yes | monthly_active | 0.03 | 0.1 | 0.35 |
| agent_search | yes | acquisition | 0.02 | 0.2 | 30.0 |
| agent_billing | no | monthly_active | 0.01 | 0.02 | 0.05 |
| agent_quality | yes | monthly_active | 0.005 | 0.02 | 0.1 |
| agent_triage | yes | monthly_active | 0.001 | 0.001 | 0.02 |
