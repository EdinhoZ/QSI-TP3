# VNF Architecture Fix - Complete Summary

## CRITICAL ISSUES IDENTIFIED

### 1. **Classifier was on WRONG interface**
   - **Problem**: Ran on OVS bridge `s1` instead of host interfaces
   - **Impact**: DSCP marking happened too late; traffic might bypass the switch
   - **Fix**: Now applies iptables DSCP marking in each **host namespace** OUTPUT chain

### 2. **Policer/Scheduler targeted BRIDGE ports instead of HOSTS**
   - **Problem**: Applied tc to bridge ports (wrong end of the pipe)
   - **Impact**: Traffic control never affected actual host traffic
   - **Fix**: Now applies tc to **host interfaces** (`h1-eth0`, `h4-eth0`, etc.)

### 3. **Conflicting tc qdiscs**
   - **Problem**: Tried to have both HTB (policer) and prio (scheduler) as root qdiscs
   - **Impact**: Only one would be active; the other ignored
   - **Fix**: **Integrated design**: HTB as root with prio as child qdiscs

### 4. **Firewall ineffective in Mininet**
   - **Problem**: iptables FORWARD chain doesn't see OVS L2-switched traffic
   - **Impact**: Firewall rules had no effect
   - **Status**: Kept for completeness but may not function in pure L2 topology

## NEW ARCHITECTURE

### How it works now:

```
┌─────────────────────────────────────────────────────────┐
│  HOST NAMESPACE (h1, h4, etc.)                          │
│                                                          │
│  1. Application sends packet                            │
│     ↓                                                    │
│  2. CLASSIFIER (iptables mangle OUTPUT)                 │
│     - UDP 5004/5005 → DSCP 46 (RTP/streaming)          │
│     - TCP 80 → DSCP 34 (HTTP)                          │
│     - TCP 9000 → DSCP 0 (bulk, default)                │
│     ↓                                                    │
│  3. POLICER+SCHEDULER (tc on h*-eth0)                   │
│     ┌─────────────────────────────────┐                │
│     │ HTB (root qdisc 1:)             │                │
│     │  ├─ Class 1:10 (20mbit) ← DSCP 46  │            │
│     │  │   └─ prio 10: (prioritization) │            │
│     │  ├─ Class 1:20 (10mbit) ← DSCP 34  │            │
│     │  │   └─ prio 20:                   │            │
│     │  └─ Class 1:30 (5mbit) ← default    │            │
│     │      └─ prio 30:                   │            │
│     └─────────────────────────────────┘                │
│     ↓                                                    │
│  4. Packet exits via h*-eth0 to OVS bridge              │
└─────────────────────────────────────────────────────────┘
```

### What each component does:

1. **CLASSIFIER (iptables)**:
   - Sets DSCP field in IP header based on port number
   - Applied in OUTPUT chain of each host's mangle table
   - Runs **before** tc processes the packet

2. **POLICER (HTB tc qdisc)**:
   - Creates 3 rate-limited classes with different bandwidth allocations
   - DSCP 46 (streaming) gets 20mbit
   - DSCP 34 (HTTP) gets 10mbit
   - DSCP 0 (bulk) gets 5mbit

3. **SCHEDULER (prio as child of HTB)**:
   - Provides prioritization **within** each rate class
   - Ensures streaming packets go first even under congestion
   - Works hierarchically with HTB rate limits

## TESTING THE FIX

### Step 1: Verify clean state
```bash
python3 verify_vnfs.py
```
Should show "noqueue" and empty iptables mangle tables.

### Step 2: Activate VNFs
```bash
sudo python3 test_script.py
```
You should see:
```
[ORC] Applying HTB+Prio on h1 (h1-eth0)
[ORC] Applying HTB+Prio on h2 (h2-eth0)
...
[ORC] DSCP marking applied in h1
[ORC] DSCP marking applied in h2
...
```

### Step 3: Verify VNFs are active
```bash
python3 verify_vnfs.py
```
Should now show:
- HTB qdisc with classes 1:10, 1:20, 1:30
- Filters matching TOS values (DSCP << 2)
- iptables mangle rules setting DSCP

### Step 4: Run traffic test
```bash
sudo python3 backup.py
# OR
sudo python3 stress_test.py --clients h4
```

### Step 5: Compare with/without VNFs

**WITHOUT VNFs** (baseline):
- All traffic should get similar throughput
- Limited only by link capacity
- No prioritization

**WITH VNFs** (test_script.py running):
- Streaming (UDP 5004/5005): ~20 Mbps, 0% loss
- HTTP (TCP 80): ~10 Mbps
- Bulk (TCP 9000): ~5 Mbps
- Clear prioritization under congestion

## KEY CHANGES TO test_script.py

1. **Removed bridge-based policer/scheduler**
   - Old: Applied tc to bridge ports
   - New: Apply to host interfaces

2. **Integrated policer+scheduler**
   - Old: Separate, conflicting qdiscs
   - New: HTB with prio children

3. **Fixed classifier location**
   - Old: OVS flows on bridge
   - New: iptables mangle in host namespaces

4. **Simplified VNF launch**
   - classifier, policer, scheduler are now inline configuration
   - Only monitor and firewall run as separate processes
   - Much cleaner and more reliable

## WHY THIS FIXES THE PROBLEM

### Before (broken):
```
Host h1 → [no DSCP marking] → OVS s1 → [tc on bridge port - wrong!] → Host h4
```
- DSCP never set (classifier on wrong interface)
- tc on bridge port doesn't affect host traffic
- No observable difference with/without VNFs

### After (fixed):
```
Host h1 → [iptables: set DSCP] → [tc: rate limit by DSCP] → OVS s1 → Host h4
```
- DSCP set in host where packet originates
- tc applied where traffic actually flows
- VNFs now DIRECTLY control the traffic

## VALIDATION

Run this sequence to prove VNFs work:

```bash
# 1. Clean state - get baseline (should be ~Gbit/s for all)
sudo python3 backup.py

# 2. Activate VNFs
sudo python3 test_script.py &
sleep 2

# 3. Test with VNFs - should see clear rate limits
sudo python3 backup.py

# 4. Stop VNFs
fg  # bring test_script to foreground
Ctrl+C

# 5. Verify cleanup
python3 verify_vnfs.py  # should show clean state again
```

Expected results in step 3:
- RTP: ~20 Mbps (up to rate limit)
- HTTP: ~10 Mbps (rate limited)  
- Bulk: ~5 Mbps (rate limited)

This is COMPLETELY DIFFERENT from step 1, proving VNFs work!
