# סשן 6 — סוכן ההתאמה

הקלט הוא בסיס קורות חיים מאושר ומודעה מנותחת. הפלט הוא מסמך וורד בתיקייה על שם המשרה, עם שם קובץ ניטרלי.

---

## הרעיון שקובע הכל: המודל מחזיר שינויים, לא מסמך

התאמה איננה כתיבה מחדש. ששת הבסיסים כבר אושרו, ומה שהמודעה לא שואלת עליו נשאר זהה בית-בית. לכן היחידה היא **שינוי** ולא מסמך:

```
op            which allowed operation
section       which line
before        what the base says there
after         what it should say
source        base | inventory
source_line   where it came from
```

שינוי בלי מקור מפיל את הריצה. זה מה שמחליף תקרה מספרית על כמות השינוי — נועם קבע שספירת שורות היא האילוץ הלא נכון, כי מודעה אחת עשויה בצדק לגעת ביותר שורות מאחרת.

---

## אחד עשר האיסורים, כל אחד בדיקה ומבחן

```
edit_summary              the summary is byte-identical across every application
add_new_bullet            no new line, ever
introduce_number          no figures; a number occupies keyword space
exceed_one_page           measured against the base, since there is no Word here
drop_or_weaken_anchor     every anchor survives with its claim intact
claim_without_evidence    every word traces to the base or to the inventory
fischer_adoption_claim    no adoption or production claim on Fischer work
attribute_make_to_fischer Make belongs to the Growth Directorate
vertex_on_fischer         the Fischer stack is Claude Code and Firebase
write_ai_assisted         never, in any form
touch_identity_block      contact, employers, titles, dates are structural
```

יש גם מבחן שסורק את המזהים עצמם: איסור שייכנס לחוזה ולא יקבל מבחן דחייה ייפול בשמו, במקום לחכות שמישהו יזכור לכתוב לו מבחן.

---

## מה שהביקורת האדוורסרית מצאה כאן

הבדיקות היו אמיתיות והחור היה מתחתן: **אף אחד לא בדק שה-`before` של השינוי הוא באמת השורה בבסיס.** ארבעה מהאיסורים הם השוואה בין `before` ל-`after`, ולכן שינוי שהמציא `before` שכבר מכיל "in production" עבר את כולם והכניס את הטענה למסמך. אותו טריק הזיז את Vertex לפישר, את מייק לפישר, ואת "contributed" ל-"led".

עוד ארבעה מאותו סבב: עוגן נקשר לשורה של מעסיק אחר לפי אוצר מילים בלבד; מחיקת שורה שנשאה טקסט חלופי הייתה שקופה לכל בדיקה ברמת המסמך; ירידת שורה בתוך `after` יצרה שורה חדשה אמיתית שהערכת העמוד ספרה כאחת; ושם חברה שנשלף מהאתר והוא נקודה בודדת כתב את המסמך לתוך תיקיית הבסיסים עצמה.

**ומבחן אחד לא יכול היה להיכשל:** "העיצוב שרד את העריכה" נכתב על פסקה בעלת ריצת טקסט אחת, כלומר הוא החזיק גם כשכל העיצוב נמחק.

---

## הרצה

```bash
uv run desk tailor --fingerprint <fp>
uv run desk tailor --fingerprint <fp> --write
```

ריצה יבשה מדפיסה כל שינוי, את הראיה שלו, ואת הפערים — מה שהמודעה ביקשה ושהמסמך לא טוען. `--write` כותב, ומסרב לדרוס מסמך קיים בלי `--force`, כי הפורמט הוא וורד בדיוק כדי שתערוך אותו אחר כך ביד.
