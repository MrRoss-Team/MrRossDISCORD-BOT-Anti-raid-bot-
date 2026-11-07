# 🛡️ Tarcza – bot anty-nuke, który sam odbudowuje serwer

Zwykłe boty moderacyjne banują za spam albo brzydkie słowa. Tarcza robi coś trudniejszego:
**gdy ktoś (np. ze skradzionym kontem admina) zaczyna niszczyć serwer, bot w kilka sekund go blokuje
i sam odbudowuje wszystko, co zostało zniszczone.**

## Co potrafi

| Funkcja | Jak działa |
|---|---|
| **Wykrywanie ataku** | Śledzi dziennik zdarzeń na żywo i liczy niszczące akcje *jednej osoby* (usuwanie kanałów/ról, bany, wyrzucenia, webhooki, dodawanie botów…). Po przekroczeniu progu, np. 3 usunięte kanały w 15 s, uznaje to za atak. |
| **Kara** | Zabiera sprawcy wszystkie role (domyślnie), wyrzuca go albo banuje. Złośliwe boty zawsze dostają bana. |
| **Automatyczne cofanie** | Cofa *wszystko*, co sprawca zrobił w ostatnich 10 min: odtwarza kanały i kategorie (z tymi samymi uprawnieniami, opisami, kolejnością), odtwarza role i oddaje je tym samym ludziom, przywraca uprawnienia ról na wszystkich kanałach, odbanowuje, kasuje spamowe kanały/role/webhooki, przywraca nazwy, nicki i ustawienia serwera. |
| **Odtwarzanie wiadomości** | Discord kasuje wiadomości razem z kanałem. Tarcza trzyma w RAM 50 ostatnich wiadomości z każdego kanału i po odbudowie wrzuca je z powrotem przez webhook, z nickiem i awatarem autora. |
| **Straż uprawnień** | Gdy ktoś spoza zaufanych da roli Administratora (lub inne groźne uprawnienie) albo nada komuś taką rolę, bot od razu to cofa. |
| **Blokada botów** | Bot dodany przez kogoś spoza zaufanych zostaje od razu wyrzucony. |
| **Anty-rajd z wykrywaniem klonów** | Oprócz fali wejść wykrywa „rodziny” kont założonych hurtowo (podobne nazwy, ta sama godzina założenia, domyślne awatary). Działa też na powolne rajdy. Wstrzymuje zaproszenia, podnosi weryfikację, usuwa konta z rajdu i sam to wyłącza po 5 min spokoju. |
| **Kopie zapasowe** | Co 30 min zapisuje strukturę serwera (role, kanały, uprawnienia, kto ma jakie role). `/kopia przywroc` odtwarza wszystko, czego brakuje, nawet jeśli bota nie było online w czasie ataku. |
| **Ręczne cofanie** | `/cofnij @osoba 60` cofa wszystko, co ta osoba zrobiła w ciągu ostatniej godziny (do doby wstecz). |
| **Poziomy i karta awansu** | Za pisanie wiadomości wpada exp (domyślnie 10–20 za wiadomość, najwyżej raz na minutę, 250 na poziom). Po awansie bot wysyła obrazek „Awans poziomu”: ciemna karta z zieloną gwiazdką, awatarem, nazwą, nowym poziomem, expem i stopką z ikoną serwera. |

## Instalacja (ok. 5 minut)

1. Wejdź na <https://discord.com/developers/applications> → **New Application** → nadaj nazwę.
2. Zakładka **Bot** → **Reset Token** → skopiuj token. Nikomu go nie pokazuj.
3. Na tej samej stronie w **Privileged Gateway Intents** włącz:
   - **SERVER MEMBERS INTENT** (wymagane),
   - **MESSAGE CONTENT INTENT** (do odtwarzania wiadomości; jeśli nie chcesz, ustaw `ODTWARZAJ_WIADOMOSCI=0` w `.env`).
4. Zaproś bota (wstaw ID aplikacji z zakładki *General Information*):
   `https://discord.com/oauth2/authorize?client_id=TWOJE_ID&scope=bot+applications.commands&permissions=8`
5. **Ważne:** Ustawienia serwera → Role → **przeciągnij rolę bota na samą górę**. Bot nie ukarze nikogo, kto ma wyższą rolę.
6. Uruchom `start.bat`. Za pierwszym razem zainstaluje biblioteki i otworzy plik `.env`: wklej token, zapisz i uruchom `start.bat` ponownie.
7. Na serwerze wpisz:
   - `/tarcza logi #kanał-adminów` – kanał na raporty,
   - `/tarcza status` – sprawdzenie, czy wszystko jest dobrze ustawione,
   - `/tarcza zaufani dodaj @osoba` – zaufani admini (Tarcza ich nie blokuje).

> Komendy globalne mogą pojawić się z kilkuminutowym opóźnieniem. Jeśli chcesz je mieć od razu, wpisz ID serwera w `SERWER_ID` w `.env`.

## Komendy

| Komenda | Kto | Co robi |
|---|---|---|
| `/tarcza status` | admin | stan konfiguracji, ustawienia, progi, kopie |
| `/tarcza logi` | zaufani | kanał na raporty |
| `/tarcza zaufani` | właściciel | lista osób, których bot nie blokuje |
| `/tarcza kara` | właściciel | zabranie ról / wyrzucenie / ban |
| `/tarcza prog` | właściciel | np. „usuwanie kanałów: 5× w 10 s” |
| `/tarcza opcja` | właściciel | włączanie i wyłączanie funkcji |
| `/kopia zrob` / `lista` / `porownaj` | zaufani | kopie zapasowe |
| `/kopia przywroc` | właściciel | odtwarza brakujące role i kanały z kopii |
| `/rajd start` / `koniec` | zaufani | ręczne włączanie i wyłączanie trybu anty-rajd |
| `/rajd ustaw` | właściciel | progi rajdu i co robić z kontami |
| `/cofnij` | zaufani | cofa zmiany wybranej osoby z ostatnich X minut |
| `/poziom [@osoba]` | każdy | karta z poziomem, expem i miejscem w rankingu |
| `/ranking` | każdy | top 10 osób z najwyższym poziomem |
| `/poziomy kanal [#kanał]` | zarządzanie serwerem | gdzie wysyłać karty awansu (puste = tam, gdzie ktoś napisał) |
| `/poziomy ustaw` | zarządzanie serwerem | exp za wiadomość, exp na poziom, przerwa, tekst stopki, oznaczanie @ |
| `/poziomy nadaj` | zarządzanie serwerem | ustawia komuś poziom (np. przy przenoszeniu z innego bota) |
| `/poziomy test` | zarządzanie serwerem | podgląd karty awansu, widoczny tylko dla ciebie |

Ustawienia ochrony zmienia **tylko właściciel serwera**, żeby przejęte konto admina nie mogło wyłączyć ochrony.

## Karta awansu – czcionka i podgląd

Karta używa czcionki **DejaVu Sans**, jeśli ją znajdzie: w folderze `fonts/` w projekcie albo w systemie (na Linuksie zwykle jest).
Na Windowsie bez DejaVu bot bierze **Verdanę**, która wygląda prawie tak samo. Żeby karta była identyczna jak wzór,
wrzuć pliki `DejaVuSans.ttf` i `DejaVuSans-Bold.ttf` do folderu `fonts/` (to darmowa czcionka).

Podgląd bez uruchamiania bota: `.venv\Scripts\python.exe -m tarcza.cards` zapisze plik `podglad_karty.png`.
Poziomy są zapisane w `data/levels.db`, więc restart bota ich nie kasuje.

## Jak to bezpiecznie przetestować

1. Załóż testowy serwer i dodaj na nim bota.
2. Daj drugiemu kontu rolę z Administratorem. Nie dodawaj go do zaufanych, a jego rola musi być **pod** rolą bota.
3. Z drugiego konta szybko usuń 3 kanały. Bot powinien w kilka sekund zabrać mu role, odtworzyć kanały z wiadomościami i wysłać raport.

Testy logiki (bez łączenia z Discordem): `.venv\Scripts\python.exe -m unittest discover -s tests`

## Ograniczenia (wynikają z Discorda)

- **Właściciel serwera** jest poza kontrolą bota. Jeśli przejmą jego konto, pomoże tylko `/kopia przywroc` po odzyskaniu konta.
- Jeśli sprawca ma rolę **wyżej niż bot**, bot nie może go ukarać. Wtedy cofa jego zmiany na bieżąco, ale lepiej trzymać rolę bota na górze.
- Jeśli atakujący **najpierw wyrzuci bota**, bot nie zdąży zareagować. Wtedy pomoże `/kopia przywroc` po ponownym dodaniu (kopie zostają na dysku).
- Wiadomości: odtwarza się tylko ostatnie 50 na kanał, bez załączników (Discord kasuje je razem z kanałem). Pamięć jest tylko w RAM, ale po restarcie bot sam dociąga ostatnie wiadomości.
- Nie da się cofnąć wyrzuceń (ludzie muszą wejść ponownie), usuniętych emoji ani zmiany ikony serwera. Raport to wypisuje.
- Odtworzone kanały i role dostają **nowe ID** (tak działa Discord). Bot pamięta mapowanie, więc kolejne przywracanie się nie dubluje.

## Pliki

```
bot.py               uruchomienie bota
tarcza/state.py      żywy obraz serwera i „cmentarz” usuniętych rzeczy
tarcza/antinuke.py   wykrywanie ataku, kara, straż uprawnień
tarcza/restore.py    odtwarzanie kanałów i ról, cofanie wpisów z dziennika zdarzeń
tarcza/messages.py   pamięć i odtwarzanie wiadomości
tarcza/antiraid.py   anty-rajd i wykrywanie kont-klonów
tarcza/snapshots.py  kopie zapasowe w data/<id_serwera>/snapshots/
tarcza/commands.py   komendy /tarcza, /kopia, /rajd, /cofnij
tarcza/levels.py     exp za wiadomości, awanse, /poziom, /ranking, /poziomy
tarcza/cards.py      rysowanie karty „Awans poziomu” (Pillow)
```
