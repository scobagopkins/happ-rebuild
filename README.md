# happ-rebuild

Сборка Happ в GitHub Actions: официальный APK + минимальная правка манифеста,
чтобы приложение ставилось рядом с официальным.

- `axml_rename.py` — правит строку applicationId прямо в бинарном
  AndroidManifest.xml (пул строк), не трогая dex, ресурсы и нативы.
  Требование: новое имя той же длины, что и `su.happ.proxyutility` (20).
- `.github/workflows/build.yml` — download → rename → проверки → подпись →
  артефакт + релиз.
- `keystore/happmod.jks` — ключ подписи, alias `happmod`, пароль `happmod123`.

Локально:

```
python3 axml_rename.py stock.apk out.apk --new-id su.happ.proxymodapp1
java -jar uber-apk-signer.jar --apks out.apk --ks keystore/happmod.jks \
  --ksAlias happmod --ksPass happmod123 --ksKeyPass happmod123 --allowResign
```
