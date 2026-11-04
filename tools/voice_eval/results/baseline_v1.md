# Classifier baseline v1

- Classifier commit base: `876988ed963c1e72c403a2e77361c1cc5fc1e6d8`
- Classifier SHA256: `839f600207a39532d2d60a1216af2fcd836b4b3f40b3ddc41d9d78076656682b`

## First held-out evaluation

### Base set

| Class | Result |
| --- | ---: |
| coke | 25/25 (100%) |
| tissue | 25/25 (100%) |
| snack | 20/25 (80%) |
| unknown | 25/25 (100%) |
| **Overall** | **95/100 (95%)** |

### Hard set

| Class | Result |
| --- | ---: |
| coke | 3/5 (60%) |
| tissue | 5/5 (100%) |
| snack | 2/5 (40%) |
| unknown | 4/5 (80%) |
| **Overall** | **14/20 (70%)** |

After this evaluation, the 120 evaluated cases were renamed to
`dev_cases_v1.json` and are treated only as development/diagnostic data.
