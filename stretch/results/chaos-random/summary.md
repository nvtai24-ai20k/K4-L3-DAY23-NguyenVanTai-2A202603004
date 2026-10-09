| run | mode | kill_after_s | first_fail_s | detect_s | cutover_s | rto_s | verdict | rpo_s | docs_lost | requests_failed | valid | warnings |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | stop | 8.3 | 0.3 | 11.7 | 18.5 | 21.8 | PASS | 24.0 | 12 | 43 | True | 0 |
| 2 | stop | 17.8 | 0.2 | 12.2 | 18.5 | 22.7 | PASS | 4.0 | 2 | 45 | True | 0 |
| 3 | netblock | 20.7 | 0.3 | 18.3 | 26.5 | 28.6 | PASS | 14.0 | 7 | 13 | True | 0 |
| 4 | netblock | 13.8 | 0.2 | 15.9 | 23.2 | 27.2 | PASS | 2.0 | 1 | 12 | True | 0 |
| 5 | netblock | 14.7 | 0.3 | 15.1 | 23.3 | 25.3 | PASS | 4.0 | 2 | 11 | True | 0 |

| Nhóm | n | mean RTO | stdev | min | max |
|---|---|---|---|---|---|
| tất cả | 5 | 25.12s | 2.89s | 21.8s | 28.6s |
| stop | 2 | 22.25s | 0.64s | 21.8s | 22.7s |
| netblock | 3 | 27.03s | 1.66s | 25.3s | 28.6s |
