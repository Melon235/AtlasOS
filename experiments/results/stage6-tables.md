# Stage 6 Generated Tables

## Table A - Stable layout

| renderer | completion order | rendered order | layout hash | invariant |
|---|---|---|---|---|
| naive_execution_order | a_first | research-a, research-b | `ded6ce5315ee51d9...` | False |
| naive_execution_order | b_first | research-b, research-a | `a97dffed69e76717...` | False |
| stable_deterministic | a_first | research-a, research-b | `ded6ce5315ee51d9...` | True |
| stable_deterministic | b_first | research-a, research-b | `ded6ce5315ee51d9...` | True |

## Table B - Dedup scaling (offline)

| scale | duplicate ratio | T1 char reduction | T0 latency ms | T1 latency ms |
|---|---:|---:|---:|---:|
| small | 0.000 | 0.0% | 0.00 | 1.20 |
| small | 0.167 | 9.6% | 0.00 | 0.94 |
| small | 0.310 | 7.4% | 0.00 | 0.89 |
| small | 0.500 | 7.6% | 0.00 | 1.05 |
| medium | 0.000 | 0.0% | 0.00 | 3.27 |
| medium | 0.167 | 11.2% | 0.00 | 3.23 |
| medium | 0.310 | 20.8% | 0.00 | 3.14 |
| medium | 0.500 | 32.3% | 0.00 | 3.06 |
| large | 0.000 | 0.0% | 0.00 | 7.45 |
| large | 0.167 | 14.7% | 0.00 | 7.78 |
| large | 0.310 | 27.6% | 0.00 | 7.53 |
| large | 0.500 | 44.9% | 0.00 | 6.68 |

## Table B live - Dedup scaling (DeepSeek)

| scale | duplicate ratio | T0 prompt | T1 prompt | prompt reduction | T0 warm miss | T1 warm miss | warm miss reduction | T0 weighted hit | T1 weighted hit |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| small | 0.000 | 32,920 | 32,920 | 0.0% | 736 | 736 | 0.0% | 0.9721 | 0.9721 |
| small | 0.310 | 32,745 | 30,600 | 6.6% | 596 | 928 | -55.7% | 0.9772 | 0.9621 |
| small | 0.500 | 35,920 | 33,495 | 6.8% | 576 | 684 | -18.8% | 0.9800 | 0.9745 |
| medium | 0.000 | 116,120 | 116,120 | 0.0% | 736 | 736 | 0.0% | 0.9921 | 0.9921 |
| medium | 0.310 | 115,975 | 92,515 | 20.2% | 620 | 796 | -28.4% | 0.9933 | 0.9892 |
| medium | 0.500 | 117,920 | 81,140 | 31.2% | 640 | 912 | -42.5% | 0.9932 | 0.9860 |
| large | 0.000 | 261,220 | 261,220 | 0.0% | 592 | 592 | 0.0% | 0.9972 | 0.9972 |
| large | 0.310 | 262,135 | 191,070 | 27.1% | 812 | 792 | 2.5% | 0.9961 | 0.9948 |
| large | 0.500 | 260,920 | 146,240 | 44.0% | 864 | 768 | 11.1% | 0.9959 | 0.9934 |

## Table C - Placement scaling

| topology | chain length | policy | switches | handoff refs | mean locality |
|---|---:|---|---:|---:|---:|
| P2 | 2 | capability_only | 4 | 4 | 0.000 |
| P2 | 2 | context_locality | 2 | 2 | 0.667 |
| P3 | 3 | capability_only | 6 | 6 | 0.000 |
| P3 | 3 | context_locality | 2 | 2 | 0.800 |
| P5 | 5 | capability_only | 10 | 10 | 0.000 |
| P5 | 5 | context_locality | 2 | 2 | 0.889 |

## Table D - Placement x dedup

| variant | policy | transform | handoff | switches | locality | prompt | cache miss | transform ms |
|---|---|---|---:|---:|---:|---:|---:|---:|
| A0 | capability_only | identity | 6 | 6 | 0.000 | 23,195 | 155 | 0.00 |
| A1 | context_locality | identity | 2 | 2 | 0.800 | 23,195 | 155 | 0.00 |
| A2 | capability_only | contextpilot_dedup | 6 | 6 | 0.000 | 18,503 | 199 | 3.43 |
| A3 | context_locality | contextpilot_dedup | 2 | 2 | 0.800 | 18,503 | 199 | 3.54 |

## Table E - Recovery

| scenario | completed reexecuted | in-flight reexecuted | rebuilt blocks | layout equivalent | final equivalent |
|---|---:|---:|---:|---|---|
| R0 | 0 | 0 | 0 | - | - |
| R1 | 0 | 1 | 1 | True | True |
| R2 | 0 | 0 | 2 | True | True |
