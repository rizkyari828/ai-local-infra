# Hardware profile

`hardware/current-machine.yaml` records facts for reproducibility rather than global defaults. The current host was reported with 32 GiB RAM; WSL exposed approximately 15 GiB during capture. The GPU is an RTX 4060 Ti with 16 GiB VRAM. This makes one substantial 27B workload at a time the operational guardrail.

Profiles name the hardware profile used for their current recommendation, but remain portable data. A new machine should receive a new hardware profile and evidence-based context/model recommendations. Do not silently mutate `current-machine-v1` to make results appear comparable.

Runtime capture found Ollama 0.34.1, Qwen Code 0.24.0, Node 24.21.0, and npm 11.19.0. The project expected .NET 10.0.401, but the shell used for capture could not resolve it; that is an environment observation, not a local-ai-infra defect.
