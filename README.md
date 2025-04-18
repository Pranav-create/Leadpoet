# Leadpoet | Premium Sales Leads. Powered by Bittensor.

Welcome to LeadPoet, a decentralized lead generation subnet built on Bittensor—tailored for SaaS, finance, healthcare, e-commerce, and B2B agencies seeking accurate, high-conversion sales leads.

## Overview

LeadPoet transforms lead generation through Bittensor’s decentralized architecture, incentivizing contributors to generate, validate, and deliver high-conversion leads in a scalable marketplace. Miners earn rewards for providing high-quality lead batches, validators earn rewards for maintaining network integrity by auditing lead quality, and buyers access curated, real-time leads optimized for conversion, eliminating reliance on static databases. Decentralization ensures cost efficiency, quality validation, and competitive sourcing—delivering fresher, more relevant leads to buyers.

- **Miners**: Run neurons that generate batches of leads in response to validator queries or buyer requests.
- **Validators**: Query miners, validate lead quality, and assign rewards based on accuracy and relevance.
- **Buyers**: Request leads via the API, receiving curated lists from approved miner submissions.

**Data Flow**: Buyers submit a query → Miners generate leads → Validators audit and score → Highest scoring list is delivered to Buyer.

**Token**: TAO is used for staking, rewards, and (future) lead purchases.

> 🧪 *Leadpoet is currently live on testnet as we refine validation and incentive mechanisms ahead of mainnet launch.*


## Getting Started

### Prerequisites

- **Hardware**: 16GB RAM, 4-core CPU, 100GB SSD.
- **Software**: Python 3.9+, Bittensor CLI.
- **TAO Wallet**: Required for staking, rewards, and (future) lead purchases.

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

**Configure Your Wallet**: Use (`btcli wallet create`) to create a TAO wallet and stake sufficient TAO.

**Set API Keys (Optional for Miners)**: To enable real lead generation using Hunter.io and Clearbit, set these environment variables:
```bash
export HUNTER_API_KEY=your_hunter_api_key
export CLEARBIT_API_KEY=your_clearbit_api_key
```
These are utilized in miner_models/get_leads.py for determining lead authenticity.


## For Miners

### Participation Requirements
Miners must stake at least 2 TAO and register on the subnet to begin submitting leads:
Use `btcli stake --amount 2 --wallet.name <your_coldkey> --wallet.hotkey <your_hotkey>`  
Then register with:  
`btcli subnet register --netuid 343 --subtensor.network test --wallet.name <your_coldkey> --wallet.hotkey <your_hotkey>`

### Lead Generation & Monitoring
Run your miner:
```bash
leadpoet --wallet.name <your_coldkey> --wallet.hotkey <your_hotkey> --netuid 343
```
Add `--use_open_source_lead_model` to use Leadpoet's open-source miner framework (optional). The miner framework requires `HUNTER_API_KEY` and `CLEARBIT_API_KEY` to be set.

Miners respond to buyer queries with lead batches. Logs show validator activity, query details, and maximum response times (must be within 2 minutes + 2 seconds per lead).

### Lead Format:
Leads must follow the structured JSON format below:

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
### Miner Incentives

Miner performance is continuously evaluated to ensure that buyers receive high-quality, conversion-ready leads.

**Accuracy Score (A_m)**: Reflects miner `m` quality of submitted leads over time:
- Recent (previous 14 days): 55% weight
- Mid-term (previous 30 days): 25% weight
- Long-term (previous 90 days): 20% weight

**Dependability (D_m)**: Reflects miner `m` reliability:
- Based on uptime: # of buyer queries responded to with valid leads divided by total # of received queries.
- Weighted by same 14/30/90-day proportions

**Miner Weight (W_m)**: `W_m = A_m × D_m`

**Rewards (M_m)**: Reflects miner `m` proportional share of total miner emissions `M`:
- Only miners with `A_m > 0.5` are eligible to receive emissions
- `M_m = M × ( W_m / Σ W_m )` for all miners with `A_m > 0.5`

**Best Practices**: Focus on accurate contact data, align with requested industry and region, and maintain high uptime to improve performance scores and maximize rewards.


## For Validators

### Participation Requirements
Validators must stake at least 20 TAO and register on the subnet to begin scoring leads:  
Use `btcli stake --amount 20 --wallet.name <your_coldkey> --wallet.hotkey <your_hotkey>`  
Then register with:  
`btcli subnet register --netuid 343 --subtensor.network test --wallet.name <your_coldkey> --wallet.hotkey <your_hotkey>`

### Running a Validator

```bash
leadpoet-validate --wallet.name <your_coldkey> --wallet.hotkey <your_hotkey> --netuid 343
```
Add `--use_open_source_validator_model` to use Leadpoet's open-source validation framework (optional).

**Monitor Your Validator**:

Validators receive lead batches, sample a percentage based on miner accuracy, complete validation within 2 minutes, and assign `O_v` scores.

Logs show validation results and reward assignments.

### Validation Process

**Audit**: Sample percentage dynamically adjusts based on miner accuracy—ranging from 10% for high performers to 50% for low performers. For example, mid-range miners may have 30 out of 100 leads sampled.
- Relevance: Matches industry/region filters.
- Format: Regex (`^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$`) and email existence.
- Accuracy: Checks for disposable domains and fakes.

**Outcome**: Approve batches only if ≥90% of sampled leads meet format, accuracy, and relevance checks. Otherwise, the batch is rejected with penalty.

**Automated Subnet Checks**: After a final score `F` above 0.5 is assigned, the subnet runs post-validation checks on each lead batch:

- Invalid Lead Check: Duplicates, invalid contacts, or incorrect formats reset the batch’s score to `F` = 0.
- Collusion Check: Buyer feedback and validator scoring patterns are analyzed using PyGOD and DBScan to detect manipulation. A Collusion Score (V_c) is generated. If V_c ≥ 0.7, the validator is flagged for collusion. R_v is set to 0 for 90 days, disabling emissions. Affected buyers are also temporarily restricted from submitting queries.

### Validator Incentives

**Final score (F)**: Each validator's `v` score `O_v` is weighted by their reputation `R_v`.  
- `F = ∑ [ O_v × ( R_v / Rs_total ) ]` where `Rs_total = ∑ R_v` across all validators `v` with `R_v > 15`.
- The lead list with the highest final score is sent to the buyer.

**Precision (P_v)**: Reflects validator `v` accuracy over time. Adjusted based on:
- Correct validation: +10 (`O_v` within 10% of final score)
- Incorrect validation: –15 (if invalid leads slip through)
- Buyer feedback: up to +15 or –25

**Consistency (C_v)**: Reflects how often validator score `O_v` is close to final `F`:
- Recent (previous 14 days): 55% weight
- Mid-term (previous 30 days): 25% weight
- Long-term (previous 90 days): 20% weight

**Weighted Reputation (R_v)**: `R_v = P_v \times C_v \times F_v`.
- Where `F_v = 1` unless the validator is flagged for collusion (then `F_v = 0`)

**Rewards (V_v)**: Reflects validator `v` proportional share of total validator emissions `V`:
- Only validators with `R_v > 15` are eligible to receive emissions
- `V_v = V × (R_v / ∑ R_v)` for all validators with `R_v > 15`

**Best Practices**: Maintain high scoring precision, ensure consistency with the final score `F`, and avoid false positives to maximize rewards and reputation.


## For Buyers

### Participation Requirements

Buyers must stake at least 50 TAO to begin requesting leads:
Use `btcli stake --amount 50 --wallet.name <your_coldkey> --wallet.hotkey <your_hotkey>`

### Accessing Leads

Buyers request leads via the API and receive curated batches that have been validated for format, accuracy, and business relevance by the network. This ensures a higher likelihood of conversion compared to static lead databases or outdated directories. All leads are validated for freshness, accuracy, and buyer fit.

**Via API**:

Start the API server:
```bash
leadpoet-api --wallet.name <your_coldkey> --wallet.hotkey <your_hotkey> --netuid 343
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


## Automated Subnet Checks
After a final score `F` above 0.5 is assigned, the subnet runs post-validation checks on each lead batch:

- **Invalid Lead Check**: Duplicates, invalid contacts, or incorrect formats reset the batch’s score to `F` = 0.
- **Collusion Check**: Buyer feedback and validator scoring patterns are analyzed using PyGOD and DBScan to detect manipulation. A Collusion Score `V_c` is generated. 
- **Collusion Flag**: If `V_c` ≥ 0.7, the validator is flagged for collusion. `F_v` is set to 0 for 90 days, disabling emissions. Affected buyers are also temporarily restricted from submitting queries.


## Technical Details

### Architecture

- **Miners**: Respond to buyer queries with JSON-formatted lead batches (via neurons/miner.py).
- **Validators**: Query miners, validate leads using validator_models/os_validator_model.py and automated_checks.py, assign rewards (via neurons/validator.py).
- **Buyers**: Use Leadpoet/api/leadpoet_api.py to query lead batches.

### API Endpoints

- **POST /generate_leads**: Request leads with num_leads (1-100), optional industry and region.

### Open-Source Frameworks

- **Lead Generation** (miner_models/get_leads.py): Fetches real leads using Hunter.io/Clearbit. Enable with `--use_open_source_lead_model`.
- **Validation** (validator_models/os_validator_model.py): Checks email validity, website reachability. Enable with `--use_open_source_validator_model`.

**API Keys**: Set HUNTER_API_KEY and CLEARBIT_API_KEY for full functionality.

### Running in Mock Mode

Test the subnet locally without connecting to the Bittensor network:

**Miner**:
```bash
leadpoet --mock --wallet.name <your_mock_coldkey> --wallet.hotkey <your_mock_hotkey> --netuid 343
```

**Validator**:
```bash
leadpoet-validate --mock --wallet.name <your_mock_coldkey> --wallet.hotkey <your_mock_hotkey> --netuid 343
```

**API**:
```bash
leadpoet-api --mock --wallet.name <your_mock_coldkey> --wallet.hotkey <your_mock_hotkey> --netuid 343
```

Note: Uses dummy data and a mock subtensor.

## Roadmap

- **MVP (Testnet)**: Core lead generation, validation, and API access (currently live)
- **Next**: Governance voting, compliance auditing, trusted validator thresholds
- **Future**: Sharding, mainnet launch, and a UI at leadpoet.com for lead querying, purchase, and feedback

## Support

- **Email**: [hello@leadpoet.com](mailto:hello@leadpoet.com)
- **Website**: [https://leadpoet.com](https://leadpoet.com)
- **Issues**: [Open an issue on GitHub](https://github.com/Pranav-create/Leadpoet/issues)

## License

MIT License - see LICENSE for details.
