# GT-correction proposals from owner pass-verdicts (2026-07-10, v99-applied waivers only)

Per GOAL §8, a machine result that disagrees with GT sub-second while the owner
confirms it renders correctly is GT-noise. Applying these GT corrections
(owner-only: GT folders are read-only for the agent) turns the scene exact
WITHOUT a waiver, dropping the per-project waiver count toward the <=3
strict-PASS budget. Duplicate-instance passes can instead stay as waivers
(<=3/project) or have GT repointed to the validated instance.

## 411f73d26c1d — sub-second GT-noise corrections (3), duplicate-instance decisions (8)
- #12: GT (42.75, 45.46) -> (42.13, 45.91)  [d=(-0.62, +0.45)]
- #13: GT (72.18, 73.82) -> (72.46, 74.28)  [d=(+0.28, +0.46)]
- #16: GT (87.75, 91.30) -> (86.99, 91.40)  [d=(-0.76, +0.10)]
### duplicate-instance passes
- #4: owner-passed instance (0.00, 0.00) vs GT (702.20, 703.60) — repoint GT or keep as waiver
- #11: owner-passed instance (34.02, 36.62) vs GT (32.87, 35.20) — repoint GT or keep as waiver
- #15: owner-passed instance (79.44, 86.99) vs GT (79.54, 85.50) — repoint GT or keep as waiver
- #19: owner-passed instance (111.67, 115.06) vs GT (110.36, 114.49) — repoint GT or keep as waiver
- #35: owner-passed instance (226.85, 230.02) vs GT (227.00, 231.27) — repoint GT or keep as waiver
- #41: owner-passed instance (431.11, 432.18) vs GT (429.39, 430.50) — repoint GT or keep as waiver
- #46: owner-passed instance (551.69, 568.37) vs GT (551.60, 560.50) — repoint GT or keep as waiver
- #50: owner-passed instance (582.43, 586.25) vs GT (582.54, 584.54) — repoint GT or keep as waiver

## 5e85164d9ff8 — sub-second GT-noise corrections (7), duplicate-instance decisions (5)
- #9: GT (223.67, 225.50) -> (223.99, 225.70)  [d=(+0.32, +0.20)]
- #22: GT (408.12, 409.36) -> (408.17, 408.89)  [d=(+0.05, -0.47)]
- #27: GT (382.36, 384.43) -> (381.50, 385.00)  [d=(-0.86, +0.57)]
- #28: GT (431.85, 434.00) -> (431.80, 433.68)  [d=(-0.05, -0.32)]
- #30: GT (492.50, 495.60) -> (492.23, 496.14)  [d=(-0.27, +0.54)]
- #42: GT (708.63, 711.00) -> (708.08, 710.44)  [d=(-0.55, -0.56)]
- #44: GT (766.50, 767.77) -> (766.29, 768.10)  [d=(-0.21, +0.33)]
### duplicate-instance passes
- #1: owner-passed instance (192.29, 195.95) vs GT (193.00, 194.00) — repoint GT or keep as waiver
- #2: owner-passed instance (197.52, 198.70) vs GT (196.00, 197.00) — repoint GT or keep as waiver
- #10: owner-passed instance (228.65, 230.25) vs GT (228.00, 229.00) — repoint GT or keep as waiver
- #13: owner-passed instance (292.29, 293.59) vs GT (290.70, 291.50) — repoint GT or keep as waiver
- #16: owner-passed instance (398.01, 398.91) vs GT (68.00, 68.90) — repoint GT or keep as waiver

## 85de83ca6323 — sub-second GT-noise corrections (7), duplicate-instance decisions (6)
- #14: GT (599.20, 600.20) -> (598.98, 599.35)  [d=(-0.22, -0.85)]
- #32: GT (774.00, 775.26) -> (774.12, 775.78)  [d=(+0.12, +0.52)]
- #34: GT (791.25, 792.00) -> (790.16, 791.46)  [d=(-1.09, -0.54)]
- #43: GT (929.01, 929.76) -> (928.95, 930.09)  [d=(-0.06, +0.33)]
- #44: GT (841.50, 843.00) -> (841.92, 842.96)  [d=(+0.42, -0.04)]
- #47: GT (962.78, 964.09) -> (963.25, 964.46)  [d=(+0.47, +0.37)]
- #51: GT (1271.00, 1272.00) -> (1270.19, 1271.94)  [d=(-0.81, -0.06)]
### duplicate-instance passes
- #8: owner-passed instance (285.42, 286.29) vs GT (279.89, 280.66) — repoint GT or keep as waiver
- #9: owner-passed instance (254.37, 255.77) vs GT (251.59, 252.50) — repoint GT or keep as waiver
- #28: owner-passed instance (742.23, 743.62) vs GT (741.50, 742.32) — repoint GT or keep as waiver
- #33: owner-passed instance (18.81, 20.16) vs GT (787.24, 788.50) — repoint GT or keep as waiver
- #42: owner-passed instance (922.71, 923.77) vs GT (926.50, 927.47) — repoint GT or keep as waiver
- #52: owner-passed instance (1320.52, 1323.70) vs GT (1322.10, 1324.20) — repoint GT or keep as waiver

## dcd74148c7ec — sub-second GT-noise corrections (6), duplicate-instance decisions (1)
- #1: GT (616.30, 618.60) -> (616.33, 617.72)  [d=(+0.03, -0.88)]
- #4: GT (637.80, 638.50) -> (637.02, 637.72)  [d=(-0.78, -0.78)]
- #7: GT (644.20, 645.90) -> (643.46, 645.72)  [d=(-0.74, -0.18)]
- #9: GT (650.80, 652.50) -> (651.21, 652.50)  [d=(+0.41, +0.00)]
- #11: GT (664.00, 665.69) -> (663.54, 664.88)  [d=(-0.46, -0.81)]
- #18: GT (761.73, 770.50) -> (761.29, 770.49)  [d=(-0.44, -0.01)]
### duplicate-instance passes
- #19: owner-passed instance (773.86, 774.54) vs GT (777.00, 781.33) — repoint GT or keep as waiver
