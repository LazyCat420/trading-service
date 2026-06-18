# Phase 3: Linear State Machine Debate

V2 failed because it generated "parallel monologues." The initial V3 plan proposed peer-to-peer messaging, but that introduces messy point-to-point state management. 

The final V3 architecture implements true Multi-Agent Debate (MAD) via a highly predictable **Linear State Machine** using the `SharedDesk`.

## 1. The Linear SharedDesk Architecture
Agents do not message each other directly. Instead, they sequentially read and append typed artifacts to a central, shared state object representing the ticker's lifecycle.

## 2. The Debate Loop (Divergent to Convergent)

### Turn 1: The Bull Argument
- **Bull Agent**: Wakes up. Reads the `FundamentalReport` and `QuantReport` currently residing on the `SharedDesk`. 
- It constructs a comprehensive LONG thesis using that data. 
- It appends a `BullArgument` artifact to the `SharedDesk` and sleeps.

### Turn 2: The Bear Rebuttal
- **Bear Agent**: Wakes up. Reads the `FundamentalReport` and `QuantReport`. 
- **The Catch**: It also reads the newly appended `BullArgument` on the desk.
- The Bear's System Prompt mandates: *"You MUST directly address the specific claims made in the BullArgument."*
- It constructs a comprehensive SHORT thesis exposing the logical flaws in the Bull's argument.
- It appends a `BearRebuttal` artifact to the desk and sleeps.

### Turn 3: Final Defense
- **Bull Agent**: Wakes up again. Reads the `BearRebuttal`.
- Makes its final closing statements defending its thesis against the attack.
- Appends `BullDefense` to the desk.

## 3. Why this works
By mediating the debate through a strict, linear state machine, the system achieves the exact same adversarial pressure as peer-to-peer messaging, but it is 10x easier to debug, log, and trace. The LLMs self-correct hallucinations and surface hidden risks that a single-pass "Judge" would miss.
