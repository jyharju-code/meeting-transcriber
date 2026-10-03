# Päätösloki: Meeting Transcriber

## D0 · Koodin koti (3.10.2026)

**Päätös:** Koodin ainoa koti on tämä git-repo. App-kansio `~/.meeting-transcriber/app` on asennettu kopio, joka asennetaan reposta. App-kansioon ei tehdä `git init`.

**Perustelu:** 2.10. kehitystä tehtiin suoraan app-kansioon (tarkka.py ja max_mode.py olivat vain siellä, providers.py ja transcribe_recording.py erkanivat). Toinen repo olisi tuonut kaksi kopiota samasta koodista.

**Toteutus:** app-kansion koodi tuotu sellaisenaan haaraan `lahtotila-2-10` (lähtötila ennen muutoksia). Ei pushattu, koska repo on julkinen.

**Hylätyt:** b) `git init` app-kansioon (kaksi repoa). c) app-kansio repon klooniksi (siistein, mutta isompi muutos kerralla; voidaan palata myöhemmin).

## D0b · config.json ja sanasto.txt (3.10.2026)

**Päätös:** Oma `config.json` ja `sanasto.txt` pysyvät koneella, eivät repossa. Repoon `config.example.json` päivitetään uusilla avaimilla yleisin arvoin, ja lisätään `sanasto.example.txt`.

**Perustelu:** config sisältää koneen polut, nimen ja pilviprojektin tunnuksen. Sanastossa on oikeiden ihmisten nimiä; julkisessa repossa se olisi kontaktilista. Sanaston mekanismi on hyvä ja säilyy.

## D1 · Käsittelysijainti: yksi kytkin (3.10.2026)

**Päätös:** Yksi kytkin, kaksi asentoa:
- **MAAILMANLAAJUINEN** (oletus): paras laatu, Google AI Studio sallittu (gemini-3.5-transcribe).
- **EU**: kaikki ääni ja teksti vain Agent Platform EU:ssa (`aiplatform.eu.rep.googleapis.com`).

Käyttäjä vaihtaa kytkimen itse. Kytkimen asento näytetään selkeästi (Dashboard, HUD, litteraatin otsikko, työkansio).

**Perustelu:** Paras laatu arkeen, EU tarvittaessa (esim. luottamukselliset haastattelut). Ei otsikkoon tai sisältöön perustuvaa päättelyä.

**Tiedostettu riski:** jos kytkin unohtuu maailmanlaajuiseen asentoon, EU-aineisto käsitellään AI Studiossa. Lievennys: näkyvä merkintä.

**Hylätyt:** a) aina EU (heikompi tunnistin, hitaampi, kustannus). c) maailmanlaajuinen + palaverikohtainen merkintä (unohtunut merkintä vuotaa, litterointi odottaisi).

## D2 · Laatutaso: toinen kytkin (3.10.2026)

**Päätös:** Laatutaso on oma kytkin, kaksi asentoa: **PERUSTASO** (oletus, yksi litterointiajo) ja **HUIPPUTASO** (kaksi ajoa + vertaava malli). Kun Huipputaso on päällä, kaikki palaverit saavat sen. Otsikkoon perustuva tason nosto (`*_title_keywords`, `resolve_preset`) poistetaan.

**Laatutaso ei koskaan vaihda palvelua:** sijaintikytkin (D1) määrää missä, laatukytkin kuinka monta ajoa. Huipputaso EU-asennossa on oma toteutuksensa (ilman gemini-3.5-transcribea), ja se testataan ennen käyttöönottoa luvalla (kuluttaa krediittiä).

**Kytkimien lukitus (ehdotettu, ei vastalausetta):** työn asetukset lukitaan työkansioon. Sijainnissa tiukempi voittaa (EU, jos EU oli päällä nauhoituksen tai litteroinnin alkaessa).

**Perustelu:** sopii esim. haastattelupäiviin; kulut hallinnassa ilman otsikkopäättelyä.

**Hylätyt:** a) Huipputaso vain palaverikohtaisella painikkeella. b) Huipputaso aina kaikille (kulut).

## D3 · Kun palvelu ei vastaa (3.10.2026)

**Päätös:**
- **EU-asento:** ensisijaisesti vain EU-palvelu. Jos EU-palvelu ei vastaa, **saa vaihtaa Googlen maailmanlaajuiseen palveluun**, mutta silloin punainen lamppu palaa (D4, korjattu 3.10.).
- **Maailmanlaajuinen asento:** vaihto sallittu vain Googlen mallien välillä (esim. gemini-3.5-transcribe → gemini-3.5-flash päivärajan täyttyessä).
- **OpenAI poistetaan käytöstä kokonaan** (litterointi ja yhteenveto), myös `select()`:n `*rest`-laajennus.
- Epäonnistuessa: macOS-ilmoitus, punainen rivi Dashboardiin, uusinta kasvavalla viiveellä vuorokauden ajan. Raakaääni säilyy aina.

**Hylätyt:** b) OpenAI viimeisenä varana maailmanlaajuisessa asennossa (eri yhtiö ja laskutus, ei tarvittu). c) ei mallivaihtoa kummassakaan (pysäyttäisi arjen turhaan päivärajan täyttyessä).

## D4 · Tunnukset ja valvonta (3.10.2026)

**Päätös:**
- OpenAI-avain poistettu ohjelman avaintiedostosta (`~/.meeting-transcriber.env`). Avain säilytettiin erikseen salasananhallinnassa.
- **Ei lukkoja eikä estoja** (ei avaimen pudotusta, ei egress-porttia). EU-asennossa EU-palvelu on ensisijainen, mutta sen pettäessä käsittely saa jatkua EU:n ulkopuolella (Google, ei OpenAI). **Tarkennus 3.10.:** Omistaja vahvisti, että ulos saa vaihtaa, kunhan lamppu palaa.
- **Punainen lamppu:** jokainen ulkoinen kutsu kirjataan työkansioon (osoite, malli). Jos palaverin käsittelyssä on yksikin kutsu EU:n ulkopuolelle, Dashboard näyttää punaisen lampun ja litteraatin alkuun tulee merkintä. Sama lamppu, jos EU-tunnus ei toimi.
- EU-tunnuksena henkilökohtainen gcloud-kirjautuminen, kuten nyt.

**Hylätyt:** kahden lukon malli (avaimen pudotus + portti), palvelutili.

## D5 · Huipputaso: nopea versio ensin (3.10.2026)

**Päätös:** Kun Huipputaso on päällä, perustason litteraatti ja yhteenveto tehdään heti, ja Huipputaso korvaa ne taustalla. Väliversio jää talteen. Ilmoitus, kun Huipputaso on valmis.

**Ehto, ettei tokeneita mene hukkaan:** Huipputaso-asennossa perustaso ajetaan samassa tilassa kuin Huipputason ensimmäinen ajo (gemini-3.5-transcribe, verbatim + sanasto, sama paloittelu), ja Huipputaso käyttää tuloksen uudelleen. Lisäkulu on vain väliversion yhteenveto.

**Peruste:** koodissa arki = `smart`, Huipputason A-ajo = `verbatim` + sanasto (`tarkka.py:153`, `max_mode.py:191`), joten ilman ehtoa yksi ääniajo menisi hukkaan (~20-25 % lisää ja Transcriben päiväraja kuluisi 3 pyyntöä/pala 2:n sijaan).

**Hylätty:** b) odotetaan suoraan Huipputasoa (muistiinpanot vasta 15-30 min päästä).

## D6 · Todentaminen ennen käyttöönottoa (3.10.2026)

**Päätös:** kaikki kolme vaihetta.
1. Automaattitestit ilman oikeita kutsuja: kytkimien lukitus työhön, punainen lamppu kun kutsu lähtee EU:n ulkopuolelle, OpenAI:ta ei kutsuta, otsikko ei vaikuta mihinkään.
2. Kuivaharjoitus vanhalla tallenteella maailmanlaajuisessa perusasennossa.
3. EU-perustaso ja EU-Huipputaso oikealla 63 minuutin palaveritallenteella.

**Lupa:** Omistaja antoi luvan ajaa vaiheen 3 (kuluttaa pilvikrediittiä) kysymättä hintaa erikseen.

**Hylätyt:** b) vain 1-2 (EU-asento testaamatta). c) vain 1.

## D7 · Vanhojen palaverien käsittelysijainnit (3.10.2026)

**Päätös:** listaa ei koota.
