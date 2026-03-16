# The Quant Playbook for Polymarket: 6 Formulas Hedge Funds Use

**Source:** 0xRicker (@0xRicker) — "The Quant Playbook for Polymarket: 6 Formulas Hedge Funds Use to Extract Millions in 2026"

**Key Insight:** Polymarket in 2026 is not a degen playground—it's a quant battlefield. Professional funds systematically extract edges using math that works in options/futures. This playbook makes 6 core formulas accessible to retail traders.

---

## Formula 1: LMSR Pricing Model (The Core Engine)

**What It Is:**
Logarithmic Market Scoring Rule (LMSR) is the AMM powering Polymarket. It converts liquidity into bounded probabilities (0-1) and determines price impact for trades.

**Formula:**
```
Price_i = e^(q_i / b) / Σ e^(q_j / b)
```
- `q_i` = quantity vector for outcome i
- `b` = liquidity depth (smaller b = bigger edges for whales)

**Why It Matters:**
Quants pre-calculate trade impact to spot mispricings in low-liquidity pools and arb before others execute.

**Example:**
On a BTC 5-minute up/down market ($36M volume):
- `b = 100`
- Buying 10 YES shares jumps price by ~5%
- Quants pre-calc this impact to detect front-running opportunities

**Risk & Edge:**
- **Risk:** In thin pools (b < 50), whales can manipulate. Always check volume.
- **Edge:** $500+ daily on impact arb in volatile markets (e.g., esports at $2M vol)

**Implementation:**
```python
import sympy as sp
import numpy as np
import matplotlib.pyplot as plt

b = 100
q_yes = sp.symbols('q_yes')
price_yes = sp.exp(q_yes / b) / (sp.exp(q_yes / b) + sp.exp(0 / b))
price_func = sp.lambdify(q_yes, price_yes)

qs = np.linspace(0, 1000, 100)
prices = price_func(qs)

# Plot to visualize curve
plt.plot(qs, prices)
plt.xlabel('Quantity Bought (YES)')
plt.ylabel('Price')
plt.title('LMSR Pricing Curve')
plt.show()

# Test on real Polymarket b value from API
```

---

## Formula 2: Kelly Criterion (Optimal Sizing for Long-Term Growth)

**What It Is:**
Kelly maximizes geometric growth while avoiding ruin. Used by every major HF (Renaissance, Two Sigma).

**Formula:**
```
f* = (p × odds - (1 - p)) / odds
```
- `p` = your edge probability (model estimate)
- `odds` = 1/price - 1 (market odds)
- `f*` = optimal fraction of bankroll to bet

**Why It Matters:**
- No all-in bets. All-in is bankruptcy waiting to happen.
- Fractional Kelly (0.25-0.5 of f*) adds safety for volatility.
- Compounds wealth exponentially if you stay above EV edge.

**Example:**
JD Vance 2028 winner (21% odds, your model p = 25% from polls/X sentiment):
```
odds = (1 / 0.21) - 1 = 3.76
f* = (0.25 × 3.76 - 0.75) / 3.76 = 0.17 (17% of bankroll)
```
Use 0.5 × f* = 8.5% for safety. Top wallets profited $200K+ hedging this.

**Risk & Edge:**
- **Risk:** Overestimate p → bankruptcy. Halve your estimates for safety.
- **Edge:** Turns $1K into $150K over Q1 2026 with consistent +EV bets.

**Implementation:**
```python
import numpy as np

def kelly(p, odds):
    return (p * odds - (1 - p)) / odds

p = 0.25  # Your edge probability
odds = (1 / 0.21) - 1  # From market price
f_star = kelly(p, odds)
print(f"Optimal fraction: {f_star:.2%}")

# Simulate 100 bets with half-Kelly sizing
bankrolls = [1000]
for _ in range(100):
    outcome = np.random.rand() < p
    bet_size = bankrolls[-1] * (f_star / 2)  # Half-Kelly
    if outcome:
        bankrolls.append(bankrolls[-1] + bet_size * odds)
    else:
        bankrolls.append(bankrolls[-1] - bet_size)

print(f"Final: ${bankrolls[-1]:,.0f}")

# Homework: Backtest on 50 historical Polymarket resolutions
```

---

## Formula 3: Expected Value (EV) Gap (Core Mispricing Detector)

**What It Is:**
The difference between your model's true probability and the market price. Bet only when EV is positive and large enough to cover fees.

**Formula:**
```
EV = (p_true - price) × payout
payout = 1 / price
```
**Entry threshold:** EV > 0.05 (after fees ~1-2%)

**Why It Matters:**
Most contracts trade near fair value. Quants scan thousands for +EV gaps daily.

**Example:**
Iran ceasefire market:
- Market price: 47%
- Your model: 52% (from news analysis, polling)
- EV = (0.52 - 0.47) × (1 / 0.47) = 0.107 ✅ (great arb on $5M volume)

**Risk & Edge:**
- **Risk:** Model inaccuracies; validate with walk-forward testing.
- **Edge:** $300+ daily on $2K bankroll scanning geo/politics markets.

**Implementation:**
```python
import pandas as pd

# Assume df with columns: market_price, model_p
df['ev'] = (df['model_p'] - df['market_price']) * (1 / df['market_price'])
opportunities = df[df['ev'] > 0.05].sort_values('ev', ascending=False)
print(opportunities[['market_price', 'model_p', 'ev']])

# Homework: Pull Polygon data for 10 markets, compute model_p (e.g., average polls)
```

---

## Formula 4: KL-Divergence (Correlation Mispricing Scanner)

**What It Is:**
Measures "distance" between probability distributions of correlated markets. Low KL signals arbitrage opportunity.

**Formula:**
```
D_KL(P||Q) = Σ P_i log(P_i / Q_i)
```
- `P` = your model's probability vector
- `Q` = market's probability vector
- **Arb threshold:** D_KL > 0.2

**Why It Matters:**
In multi-outcome or correlated markets (e.g., 2028 candidate predictions), the probabilities should be internally consistent. Low KL = correlated markets are mispriceable against each other.

**Example:**
Vance (21%) and Newsom (17%) 2028 predictions:
- High KL → they're more independent than they should be
- Hedge portfolio → $100K extracted

**Risk & Edge:**
- **Risk:** Noise in low-volume markets causes false signals.
- **Edge:** 15% portfolio uplift in diversified bets.

**Implementation:**
```python
from scipy.stats import entropy

p = [0.21, 0.79]  # Vance yes/no (market prices)
q = [0.25, 0.75]  # Your model probabilities
kl = entropy(p, q)
print(f"KL-divergence: {kl:.4f}")
if kl > 0.2:
    print("Misprice detected! Arb opportunity.")

# Homework: Compute KL for 5 correlated PM pairs
```

---

## Formula 5: Bregman Projection (Multi-Outcome Arb Optimizer)

**What It Is:**
Hedge fund staple for scanning exponential combos (2^63 possible outcomes) to find risk-free arbs via projection onto probability polytope.

**Formula:**
```
min D_φ(μ || θ) subject to constraints
```
- `φ` = convex divergence (often KL)
- `μ` = variables to optimize
- `θ` = observed prices/probabilities
- Solves for arb marginals

**Why It Matters:**
In complex multi-outcome markets (e.g., Oscars, esports brackets), quants solve optimization to find impossible price combinations that guarantee profit.

**Example:**
Oscars Best Picture ($21M volume):
- Marginal probabilities don't sum to 1
- Projection detects arb: buy low combos, sell high
- Profit: $21M × 0.1-0.5% edge = $21K-$105K

**Risk & Edge:**
- **Risk:** High compute; use approximations for speed.
- **Edge:** $496 average profit per trade, near-zero downside.

**Implementation:**
```python
import cvxpy as cp

# Binary example
mu = cp.Variable(2)
theta = [0.5, 0.5]  # Target probabilities

# Minimize KL divergence
obj = cp.kl_div(mu[0], theta[0]) + cp.kl_div(mu[1], theta[1])
constraints = [cp.sum(mu) == 1, mu >= 0]
prob = cp.Problem(cp.Minimize(obj), constraints)
prob.solve()

print(f"Optimal distribution: {mu.value}")

# Homework: Extend to 3+ outcomes (Oscars, March Madness, etc.)
```

---

## Formula 6: Bayesian Update (Dynamic Probability Adjustment)

**What It Is:**
Updates your beliefs as new evidence arrives. Beats static models in fast-moving markets.

**Formula:**
```
P(H | E) = P(E | H) × P(H) / P(E)
```
- `H` = hypothesis (e.g., "candidate wins")
- `E` = evidence (e.g., tweet sentiment, polling data)
- Update as new data streams in

**Why It Matters:**
Markets with breaking news (geopolitics, tech, finance) move fast. Bayesian traders update mid-day and capture edges before market reprices.

**Example:**
Elon tweet sentiment market ($2M volume):
- Prior: 50% (base case)
- Evidence: Tweet gets 500K likes + bullish sentiment
- Posterior: 65% → +EV bet at 45% market price

**Risk & Edge:**
- **Risk:** Bad evidence garbles outputs. Validate data quality.
- **Edge:** +12% accuracy in volatile geo/news markets.

**Implementation:**
```python
from scipy import stats
import numpy as np

# Simple beta-binomial model
prior_successes = 1
prior_failures = 1
prior = stats.beta(prior_successes, prior_failures)

# New evidence: 7 out of 10 polls favorable
evidence_successes = 7
evidence_failures = 3

# Posterior
posterior = stats.beta(
    prior_successes + evidence_successes,
    prior_failures + evidence_failures
)

print(f"Prior: {prior.mean():.2%}")
print(f"Posterior: {posterior.mean():.2%}")

# Homework: Update on real X data (e.g., RT/like ratio by hour)
```

---

## Replicating the System: Build Your Quant Bot

### Data Setup
- Get Polygon API keys for real-time Polymarket odds/volume
- Cache historical data for backtesting

### Integration Stack
```
Python (numpy, scipy, cvxpy)
Polygon API client
Database (SQLite or PostgreSQL)
Backtest engine (walk-forward validation)
Deployment: Railway/Github Actions for cron jobs
Alerts: Telegram bot for +EV signals
```

### Backtest Workflow
1. Walk-forward on 2025 data
2. Compute EV/KL/Kelly for signals
3. Out-of-sample test (hold out final 2 weeks)
4. Measure Sharpe ratio, max drawdown, win rate

### Deployment Checklist
- [ ] Polygon API authenticated
- [ ] All 6 formulas coded + tested
- [ ] Backtest Sharpe > 1.5
- [ ] Kelly sizing configured (half-Kelly recommended)
- [ ] Risk limits: 20% max drawdown stop
- [ ] Telegram alerts for +EV trades
- [ ] Position monitoring dashboard

---

## Integration with Current Strategies

These formulas are **complementary** to existing strategies:

| Existing Strategy | How Quant Formulas Help |
|---|---|
| **High Probability** | Use EV formula (3) to confirm edge, Kelly (2) to size |
| **Safe Compounder** | Kelly (2) + Bayesian (6) for dynamic adjustments |
| **Sports Daily** | KL-Divergence (4) for multi-outcome sports combos |
| **BTC Up/Down** | LMSR (1) to pre-calc trade impact, detect arb |
| **LLM Crypto** | Bayesian (6) to update as news breaks hourly |

---

## Risk Management & Reality Check

### Overfitting
- Always out-of-sample test. 2025 data ≠ 2026 regime.
- Walk-forward with fresh windows (1-week hold-out).

### Fees & Slippage
- Fees (1-2%) erode small edges. Target EV > 5%.
- Liquidity varies by market. Check 30-min volume before trade.

### Regime Shifts
- Higher vol invalidates old models.
- Recompute Kelly monthly; adjust b parameter for LMSR.

### Ethical Note
- Betting on sensitive events (wars, disease, deaths) requires caution.
- Consider market impact: large whale positions can manipulate.

### Target Metrics
- **Sharpe ratio:** > 1.5
- **Win rate:** > 55% (depends on EV threshold)
- **Max drawdown:** < 20%
- **Profit per trade:** $100-$500 (retail), $10K+ (institutional)

---

## Next Steps (Immediate Actions)

1. **Code Formula 3 (EV)** — Easiest to implement. Scan 10 live markets today.
2. **Backtest Kelly (Formula 2)** — Use existing position data; compute optimal sizing.
3. **Build Bayesian updater (Formula 6)** — Integrate X/polling API; update every 4 hours.
4. **Implement LMSR (Formula 1)** — Pull Polygon API b values; model trade impact.
5. **Deploy bot** — Railway cron + Telegram alerts.

---

## References

- Original playbook: 0xRicker (@0xRicker)
- LMSR math: Hanson, R. (2012). "Logarithmic Market Scoring Rules"
- Kelly Criterion: Thorp, E. O. (1969). "Optimal Gambling Systems"
- KL-Divergence: Cover & Thomas (1991). "Elements of Information Theory"

---

**Status:** Add to poly-trade strategies rotation. Start with Formula 3 (EV) + Formula 2 (Kelly sizing). Full system deployment by Q2 2026.
