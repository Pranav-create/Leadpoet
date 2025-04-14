# LeadPoet | Bittensor-Powered Lead Gen

Welcome to LeadPoet, a Bittensor subnet designed to create a decentralized, privacy-compliant lead generation network. Miners generate and provide lead batches, validators ensure their quality, and buyers access high-quality leads via an API—all powered by the TAO token ecosystem.

## Overview

LeadPoet leverages Bittensor's decentralized architecture to deliver a scalable, incentivized lead marketplace. Miners earn TAO rewards for providing high-quality leads, validators maintain network integrity by auditing lead batches, and buyers access tailored leads for sales and marketing purposes.

- **Miners**: Run neurons that generate batches of leads in response to validator queries or buyer requests.
- **Validators**: Query miners, validate lead quality, and assign rewards based on accuracy and relevance.
- **Buyers**: Request leads via the API, receiving curated lists from approved miner submissions.

**Data Flow**: Miners generate leads → Validators audit and score → Approved leads are available for buyers via API.

**Token**: TAO is used for staking, rewards, and (future) lead purchases.

## Getting Started

### Prerequisites

- **Hardware**: 16GB RAM, 4-core CPU, 100GB SSD.
- **Software**: Python 3.9+, Bittensor CLI.
- **TAO Wallet**: Required for staking and network participation.

### Installation

**Install Bittensor**:
```bash
pip install bittensor
```

**Clone the Repository**:
```bash
git clone https://github.com/Pranav-create/Leadpoet
cd Leadpoet
```

**Install the LeadPoet Package**:
```bash
pip install .
```

Note: This installs all dependencies listed in setup.py, including bittensor, requests, numpy, etc.

**Set API Keys (Optional for Miners)**:

To enable real lead generation using Hunter.io and Clearbit, set these environment variables:
```bash
export HUNTER_API_KEY=your_hunter_api_key
export CLEARBIT_API_KEY=your_clearbit_api_key
```
These are utilized in miner_models/get_leads.py for fetching authentic leads.

**Configure Your Wallet**:

Set up a TAO wallet using the Bittensor CLI (`btcli wallet create`) and ensure it has sufficient TAO for staking.

## For Miners

### How to Participate

Miners run neurons that respond to queries from validators or the API by generating lead batches.

**Set Up Your Wallet and Stake TAO**:

Register your wallet on the Bittensor network and stake at least 8 TAO:
```bash
btcli stake --amount 8 --wallet.name your_wallet_name --wallet.hotkey your_hotkey_name
```

**Run the Miner Neuron**:

Start your miner with:
```bash
leadpoet --wallet.name your_wallet_name --wallet.hotkey your_hotkey_name --netuid 343
```
Replace `your_wallet_name` and `your_hotkey_name` with your wallet credentials.

`--netuid 343` specifies the LeadPoet subnet; adjust if different.

**Enable Open-Source Lead Generation (Optional)**:

To generate real leads instead of dummy data:
```bash
leadpoet --wallet.name your_wallet_name --wallet.hotkey your_hotkey_name --netuid 343 --use_open_source_lead_model
```
Requires `HUNTER_API_KEY` and `CLEARBIT_API_KEY` to be set.

**Monitor Your Miner**:

The miner listens for LeadRequest queries from validators or the API and responds with lead batches.

Check logs for query details and response times (must be within 122 seconds: 2 minutes + 2 seconds per lead).

### Lead Format

Leads should follow this JSON structure:
```json
{
    "Business": "Company Inc.",
    "Owner Full name": "John Doe",
    "First": "John",
    "Last": "Doe",
    "Owner(s) Email": "john.doe@company.com",
    "LinkedIn": "https://linkedin.com/in/johndoe",
    "Website": "https://company.com",
    "Industry": "Tech & AI",
    "Region": "US"
}
```

### Incentives

**Accuracy Score (G_i)**: Calculated over a rolling 3-month period:
- Recent (T-14 to T-0 days): 50% weight.
- Mid-term (T-30 to T-14 days): 20% weight.
- Long-term (T-90 to T-30 days): 30% weight.

Formula: $G_i = 0.5 \times \text{Recent Accuracy} + 0.2 \times \text{Mid-term Accuracy} + 0.3 \times \text{Long-term Accuracy}$

Accuracy = (Good Leads / Total Leads) per period.

**Consistency Multiplier (C_i)**: Based on uptime (queries responded to accurately):

$C_i = \min(1 + \frac{\text{Uptime %}}{100}, 2.0)$

E.g., 90% uptime → $C_i = 1.9$.

**Weighted Score (W_i)**: $W_i = G_i \times C_i$.

**Rewards**: Proportional to $W_i$ relative to all miners' weighted scores:

$\text{Reward}_i = E \times \frac{W_i}{\sum W_j}$, where ($E$) is total emissions.

**Tips**: Provide accurate emails, relevant industries/regions, and maintain high uptime to maximize rewards.

## For Validators

### How to Participate

Validators audit miner submissions to ensure quality and assign rewards.

**Set Up Your Wallet and Stake TAO**:

Register your wallet and stake sufficient TAO (amount varies; aim for competitive staking).

**Run the Validator Neuron**:

Start your validator with:
```bash
leadpoet-validate --wallet.name your_wallet_name --wallet.hotkey your_hotkey_name --netuid 343
```

**Enable Open-Source Validation (Optional)**:

Use the open-source validation model:
```bash
leadpoet-validate --wallet.name your_wallet_name --wallet.hotkey your_hotkey_name --netuid 343 --use_open_source_validator_model
```

**Monitor Your Validator**:

Queries miners for batches (default 10 leads), validates within 2 minutes, and assigns scores.

Logs show validation results and reward assignments.

### Validation Process

**Pre-Check (~30s)**: Automated script checks format, duplicates (via validator_models/automated_checks.py).

**Audit (~2 min)**: Sample 20% of leads (e.g., 20/100):
- Format: Regex (`^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$`) and email existence.
- Accuracy: Checks for disposable domains and fakes.
- Relevance: Matches industry/region filters.

**Outcome**: Approve if ≥90% of sample is valid; reject otherwise.

### Incentives

**Reputation Score (R_i)**:
- Correct validation: +5 points.
- Incorrect: -10 points.
- Buyer feedback: -25 (rating 1), -10 (2-4), +2 (5-7), +10 (7-8), +15 (9-10).
- Post-approval check failure: -20 points.

**Consistency Factor (C_i)**: $C_i = 1 + 0.025 \times \text{Streak}_i$, capped at 2.0.
Streak resets if accuracy <90%.

**Weighted Reputation (W_i)**: $W_i = R_i \times C_i$.
Validators with $R_i < 15$ receive no rewards.

**Rewards**: $\text{Reward}_i = E \times \frac{W_i}{\sum W_j}$.

## For Buyers

### Accessing Leads

Buyers request leads via the API, receiving approved batches from miners.

**Via UI (Future)**:
Planned at leadpoet.com with USD/TAO payment options.

**Via API**:

Start the API server:
```bash
leadpoet-api --wallet.name your_wallet_name --wallet.hotkey your_hotkey_name --netuid 343
```

Request leads (runs on http://localhost:5003 by default):
```bash
curl -X POST http://localhost:5003/generate_leads \
     -H "Content-Type: application/json" \
     -d '{"num_leads": 10, "industry": "Tech & AI", "region": "US"}'
```

Response example:
```json
[
    {
        "Business": "Company Inc.",
        "Owner Full name": "John Doe",
        "First": "John",
        "Last": "Doe",
        "Owner(s) Email": "john.doe@company.com",
        "LinkedIn": "https://linkedin.com/in/johndoe",
        "Website": "https://company.com",
        "Industry": "Tech & AI",
        "Region": "US"
    }
]
```

Note: Lead quality depends on miner performance, validated by the network.

## Technical Details

### Architecture

- **Miners**: Respond to LeadRequest queries with lead batches (via neurons/miner.py).
- **Validators**: Query miners, validate leads using validator_models/os_validator_model.py and automated_checks.py, assign rewards (via neurons/validator.py).
- **Buyers**: Use Leadpoet/api/leadpoet_api.py to query top miners by stake.

### API Endpoints

- **POST /generate_leads**: Request leads with num_leads (1-100), optional industry and region.

### Open-Source Models

- **Lead Generation** (miner_models/get_leads.py): Fetches real leads using Hunter.io/Clearbit. Enable with --use_open_source_lead_model.
- **Validation** (validator_models/os_validator_model.py): Checks email validity, website reachability. Enable with --use_open_source_validator_model.
- **Automated Checks** (validator_models/automated_checks.py): Post-approval verification of emails and domains.

**API Keys**: Set HUNTER_API_KEY and CLEARBIT_API_KEY for full functionality.

### Running in Mock Mode

Test the subnet locally without connecting to the Bittensor network:

**Miner**:
```bash
leadpoet --mock --wallet.name mock_wallet --wallet.hotkey mock_hotkey --netuid 343
```

**Validator**:
```bash
leadpoet-validate --mock --wallet.name mock_wallet --wallet.hotkey mock_hotkey --netuid 343
```

**API**:
```bash
leadpoet-api --mock --wallet.name mock_wallet --wallet.hotkey mock_hotkey --netuid 343
```

Note: Uses dummy data and a mock subtensor.

## Roadmap

- **MVP**: Core lead generation, validation, and API access (this repo).
- **Next**: Governance, compliance audits, trusted validator thresholds.
- **Future**: Sharding, testnet deployment, UI for buyers.

## Support

- **Email**: [hello@leadpoet.com](mailto:hello@leadpoet.com)
- **Issues**: GitHub Issues

## License

MIT License - see LICENSE for details.
