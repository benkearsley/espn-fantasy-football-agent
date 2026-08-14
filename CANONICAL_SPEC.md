# Canonical Specification: Fantasy Football Agent Manager

**Status:** Canonical product specification  
**Owner:** Ben Kearsley  
**Primary objective:** Maximize championship probability in ESPN Fantasy Football.

## 1. Purpose and scope

The Fantasy Football Agent Manager is an always-on, autonomous team-management
system for Ben's ESPN league, initially **Engine League (no evil commish)**. It
uses ESPN data only at launch, makes evidence-informed decisions that take
precedence over ESPN's own rankings, and optimizes for winning the championship
rather than conservative weekly projection accuracy.

The current-season scope begins after Ben drafts manually:

- read ESPN league, roster, schedule, player, and transaction data;
- recommend and, when authorized, execute lineup, waiver, free-agent, and
  trade actions;
- negotiate and propose trades in Ben's voice;
- monitor several times daily and respond to on-demand Telegram commands;
- keep an indefinite, season-organized decision and outcome history; and
- learn from outcomes through weekly post-mortems.

Draft automation is explicitly deferred to
`fantasy-football-k0p` for a future season.

## 2. Outcomes and non-goals

### Success

The success measure is wins and, ultimately, the league championship. The
decision objective is championship equity: accepting justified variance when it
improves title odds.

### Launch constraints

- ESPN is the only data source at launch. External news, betting, injury, and
  projection sources are out of scope until expressly added.
- The service runs continuously on Ben's Raspberry Pi.
- Raw action logs must never be committed to Git.
- No dashboard is required. Telegram is the sole user-control interface.

## 3. Agent team

One **Lead Manager** owns the final decision. It delegates work to specialists,
reviews their reasoning and dissent, and chooses the action; specialists advise
but cannot veto.

| Agent | Responsibility |
| --- | --- |
| Lead Manager | Championship-equity decisions, arbitration, execution authorization, user communications, learning updates. |
| League & Data Analyst | Retrieves and normalizes ESPN league settings, roster, schedule, scoring, player, and transaction facts. |
| Draft Strategist | Future-only; owns the deferred draft feature. |
| Lineup & Waiver Manager | Evaluates starts/sits, waivers, free agents, roster construction, and timing. |
| Trade Analyst & Negotiator | Values trades, proposes deals, tracks counterparties, and writes concise negotiations. |
| ESPN Execution & Monitoring Operator | Monitors ESPN, detects state change, executes approved/permitted actions, verifies results, and recovers safely. |
| Risk Reviewer / Challenger | Independently identifies reaches, downside, rule violations, and material uncertainty. |

Specialist reasoning is preserved with each material decision and available to
Ben via `why` and the post-mortem record.

## 4. Authority and guardrails

| Action | Authority |
| --- | --- |
| Read ESPN data, monitor, analyze, and report | Autonomous |
| Set or change a lineup | Recommend first; execute only after Ben approves. An unanswered approval expires 15 minutes before the affected player's game. |
| Waiver and free-agent additions/drops | Autonomous, then notify Ben—except a drop of a current starter or a player on bye requires notice before the drop. |
| Propose a trade | Autonomous. Keep at most one active proposal per league manager; do not repeat an offer until that manager responds. |
| Accept a trade | Ben approval required. |
| Send league messages | Autonomous within trade scope; write in Ben's voice—strong, confident, brief, pointed, and never arrogant. Do not identify the system as an agent. |
| Respond to manual ESPN action by Ben | Treat it as authoritative state; adapt immediately and never undo it automatically. |

The system must send one urgent Telegram alert on lost ESPN access or a service
crash, then retry quietly until recovery. It must not take an action whose
ESPN-side result cannot be verified.

## 5. Telegram control plane

Telegram is the mobile conversational interface. The bot accepts commands only
from Ben's configured Telegram account and ignores all other users.

Baseline commands are:

- `status` — current roster, pending decisions, health, and next deadlines.
- `run` — perform an immediate monitoring/decision cycle.
- `approve` — approve the referenced pending lineup or trade-acceptance action.
- `veto <reason>` — reject the pending action and record the rationale as
  learning context.
- `pause` / `resume` — disable or re-enable ESPN writes while monitoring
  continues.
- `why` — show the lead recommendation and specialist reasoning.
- `draft` — reserved for the deferred future draft feature.

The manager sends a daily digest plus actionable notifications judged useful by
the lead manager. Every message involving a proposed lineup action must include
an unambiguous action identifier and expiry time.

## 6. ESPN integration and authentication

Prefer a supported ESPN API where one actually supports the required operation.
Otherwise, use authenticated browser automation as the write path, isolating
all ESPN interaction behind an adapter. Unofficial read endpoints may be used
only as a non-guaranteed optimization; they must not be the sole basis for a
critical action.

Ben will complete a one-time interactive ESPN login in a temporary remote
browser session if required. Store only an encrypted, least-privilege session
state locally. The Pi is headless; its normal operation needs outbound network
access only. Tailscale is not required, though it may later be added for secure
administration.

## 7. Deployment, model, and persistence

- **Host:** always-on Raspberry Pi (`blueberrypi`), Debian 13, 64-bit, Docker
  and Python available.
- **Service behavior:** supervised, restartable services; Telegram polling is
  acceptable and avoids an inbound webhook requirement.
- **Model layer:** provider-agnostic configuration. Default to a bounded-cost
  mini model or Luna; a provider/model may be changed without application-code
  changes.
- **Secrets:** Telegram credentials, ESPN session material, and model-provider
  credentials are outside Git and encrypted/permission-restricted at rest.
- **Action ledger:** append-only local storage, retained indefinitely and
  partitioned by season. Record trigger, ESPN facts used, recommendation,
  specialist reasoning, approval/veto and reason, intended action, exact
  execution, verification, and measured outcome.

## 8. Learning and reporting

The manager adapts automatically as the season progresses. It uses the action
ledger and resulting performance to refine strategy, while retaining the
evidence that led to every change.

After Monday Night Football, publish a Tuesday weekly post-mortem in HTML. It
must cover decisions, outcomes, lessons, errors or missed opportunities, and
the resulting strategy changes for the next week. These curated reports are
durable decision context: retain them on `main` and publish them through the
GitHub Pages `pages` branch. This is the sole reporting exception to the rule
against committing raw action logs.

## 9. Initial onboarding and acceptance criteria

On Ben's instruction after the manual draft, the system must:

1. establish the ESPN authenticated session and retrieve the complete league
   settings, roster, scoring, schedule, waivers, trades, and deadlines;
2. store the league-specific configuration and confirm it to Ben on Telegram;
3. configure the Telegram allowlist and command handling;
4. establish the encrypted local ledger and service-health alerting;
5. perform a read-only dry run that produces a recommendation and `why`
   explanation without writing to ESPN; and
6. enable writes only after the dry run and guardrail behavior are verified.

The manager is ready for current-season use when it can reliably monitor ESPN,
respond to `status` and `run`, produce a traceable lineup recommendation,
honor an approval/veto, autonomously execute and verify a permitted waiver or
free-agent action, and send the required notifications.
