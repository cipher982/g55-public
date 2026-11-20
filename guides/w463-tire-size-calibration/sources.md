# Sources and prior art

Public sources for the W463 tire-size calibration guide. No WIS material, no private captures, no raw EEPROM dumps.

## The factory path and the owner reports

- [MBWorld: MB STAR/Xentry functions](https://mbworld.org/forums/g-class-w460-w461-w463/879662-mb-star-xentry-functions.html) - the tire-size list in the transfer case and the cluster.
- [MBWorld: Updating tire size](https://mbworld.org/forums/g-class-w460-w461-w463/909816-updating-tire-size.html) - a 2010 G55 owner: no custom size, the largest preset improves but does not solve.
- [MBWorld: Coding on W463](https://mbworld.org/forums/g-class-w460-w461-w463/926326-coding-w463-options-available.html) - a 2013 G63 owner: the wheel circumference is not exposed under IC or ESP.
- [MBWorld: Launch OBD Scanner, tire size](https://mbworld.org/forums/g-class-w465-w463a/887384-launch-obd-scanner-tire-size-part-1-a.html) - the 2019-and-later coding makes no difference to the compensation.
- [PointedThree: 463 with larger tires, ESP problems solved](http://pointedthree.com/disc/forums/showthread.asp?start=124&tid=9622) - the transfer-case coding and the instrument-cluster Special Equipment tire size.
- [MHH Auto: IC213 speedometer calibration](https://mhhauto.com/Thread-IC213-Speedometer-calibration) - the 2013-and-later IC213 has four tire data sets with no descriptions and no raw edit.

## The aftermarket path

- [Transporter Manufaktur: Tacho-Kalibrierkit Mercedes G-Klasse 463](https://www.transportermanufaktur-shop.de/mercedes/g-klasse/elektronik/tacho-kalibrierkit-mercedes-g-klasse-463-e11-2007/46-0154-10-voreinstellung-104) - a programmable CAN node in front of the cluster; around 550 EUR; programmed for the original and the retrofit tire.
- [SuperKilometerFilter: Mercedes G Class W463 speedometer calibration device](https://superkilometerfilter.com/product/mercedes-g-class-w463-speedometer-adjustment/) - a plug-in unit; around 400 EUR; corrects the speedometer and odometer for non-standard tires.
- [Autohankeys: Universal CAN filter for MB FBS3/FBS4](https://www.autohankeys.com/universal-can-filter-for-mb-fbs3-fbs4-compatible-w204-w222-mileage/sync-correction-module-9406) - a mileage and synchronization filter; it covers the G-Class W463 up to 06/2012, but it is not a tire calibration.
- [Mileage stopper: Mercedes-Benz G-Class W463 2012-2018](https://mileagestopper.com/mercedes-benz/mercedes-benz-g-class-w463-mileage-stopper) - a CAN blocker; an odometer tool, not a tire calibration.

## The cluster and EEPROM prior art

- [gmenounos/vwcluster: Checksums](https://github.com/gmenounos/vwcluster/blob/main/Checksums.md) - the VW cluster K-value and the checksum-protected EEPROM regions; the closest public analog.
- [gmenounos/vwcluster: Distance Impulse Number](https://github.com/gmenounos/vwcluster/blob/main/Distance%20Impulse%20Number.md) - the K-value concept and the hardcoded values.
- [UnlockECU](https://github.com/jglim/UnlockECU) - the KI203-family seed and key.
- [MHH Auto: Instrument cluster EEPROM reading and writing with Vediamo](https://mhhauto.com/Thread-Instrument-Cluster-EEPROM-reading-writing-with-VEDIAMO) - the W203/W211 EEPROM and the 0x3D write with security access.
- [Minitools: display modification for the W463 and W203](https://www.minitools.com/de/tacho-display-mercedes-g-klasse-w463-c-klasse-w203-anleitung-zur-aenderung-sepdisp06/) - the 93C86 EEPROM programming procedure for the cluster display.
- [Keyprogtools: odometer correction for W211, W219, W164, W203, W251, W463, R171, W209](https://keyprogtools.com/images/Odometer%20W211%20W219%20X164%20W164%20W203%20W251%20W463%20R171%20W209.pdf) - the cluster and CGW EEPROM locations; an odometer service, not the K-Zahl.
- [KAP Diagnostics: MN030 FBS4 instrument cluster calibration](https://www.kapdiagnostics.com/mn030-fbs4-instrument-cluster-calibration.html) - the 2013-and-later IC213/IC172 path.
- [G-Wagen Owners Association: speedo drive correction](https://www.gwoa.co.uk/content/speedo-drive-correction) and [1992 W463 bigger tyres](https://www.gwoa.co.uk/content/1992-1993-w463-short-bigger-tyres-change-angular-gear-speedometer) - the older mechanical speedometer and the angular gear.

## The local evidence

The EEPROM read, the coding read and the checksum tests are in the private vehicle workspace. This public page keeps the decoded offsets and values; the raw binary stays private.
