# Best Portable Power Stations for a Winter Outage (2026)

<!-- author-agent: draft 3, picks from v_pick_eligible at 2026-11-17T09:00Z -->

## Quick Answer

For most homes, a **2 kWh LFP station** runs a full-size fridge for about _20 hours_ and a
Wi-Fi router for days. Our pick is the [Alpha 2000](/product/B0ABCDEFGH); check the
[current price on Amazon](https://www.amazon.com/dp/B0ABCDEFGH?tag=someone-else-20&th=1).

## How we picked

We ranked stations by usable watt-hours per dollar, using manufacturer specs and the
NEC-style assumptions in [how we calculate](/how-we-calculate). Runtime figures assume an
inverter efficiency of 0.842 (`rated_wh * 0.842`), which is why a "2,048 Wh" unit shows
1,724 Wh usable.

| Station | Usable Wh | Continuous W | Chemistry |
|:--------|----------:|-------------:|:---------:|
| [Alpha 2000](/product/B0ABCDEFGH) | 1,724 | 2,200 | LFP |
| Beta Max \| Plus | 2,586 | 3,000 | LFP |
| Gamma 1000 | 862 | 1,800 | NMC |

### Runtime of common loads

Load | Watts | Hours on 1,724 Wh
--- | --- | ---
Full-size fridge | 150 avg | ~11.5
Wi-Fi router + modem | 20 | ~86
CPAP-free bedroom fan | 45 | ~38

> Never backfeed a house through a dryer outlet. Use a transfer switch installed by a
> licensed electrician.

## Buying checklist

1. Match **continuous watts** to your biggest load.
2. Prefer LFP chemistry for 3,000+ cycles.
3. Check the solar input voltage before buying panels.

- Expansion batteries: see our [compatibility hub](/compat/alpha-2000).
- Shopping elsewhere? A [UK listing](https://www.amazon.co.uk/dp/B0ABCDEFGH) and a
  [short link](https://amzn.to/3xYzAbC) are not links we can stand behind.

<div class="buystrip">
<a class="amz-cta" href="https://www.amazon.com/dp/B0ABCDEFGH?tag=old-tag-20" rel="nofollow">See the Alpha 2000 on Amazon</a>
</div>

![Alpha 2000 on a workbench](/images/alpha-2000-bench.webp)
![Amazon product shot](https://m.media-amazon.com/images/I/71abcdEFGHL._AC_SL1500_.jpg)

```bash
# sizing math, not markdown
echo $(( 2048 * 842 / 1000 ))  # **usable** Wh
```

## FAQ

### How long will a 2 kWh station run a fridge?

About 11 to 20 hours, depending on the fridge's duty cycle and the room temperature.
