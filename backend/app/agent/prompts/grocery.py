PRODUCT_FACTS_SYSTEM_PROMPT = """\
The user typed one product for their shopping list. Describe how it is
usually bought, how much one person uses at once, and how long it keeps.
Call product_facts.

* name — the product's usual shopping-list name in the language the user
    wrote, capitalized, without amount or brand, typos fixed. Plural for
    things bought as several pieces ("Tomatoes", "Eggs"), singular otherwise
    ("Rice", "Oat milk").
* unit — one unit for both amounts below:
    - g or ml for goods sold by weight or volume that get portioned out
      (rice, pasta, flour, milk, oil).
    - piece for loose things counted one by one (tomatoes, apples, eggs,
      onions).
    - pack, can, jar, bottle or bunch for things bought as one container and
      used up in parts (cherry tomatoes, tofu, chickpeas, pesto, herbs).
* pack_quantity — the amount usually bought at once, in unit, as a size
    stores actually sell: 1000 g of rice, 1000 ml of milk, 10 eggs, 1 pack of
    cherry tomatoes, 4 tomatoes.
* portion — what one person uses for one meal or one use, in unit: 150 g of
    rice, 200 ml of milk, 1 tomato, 0.5 pack of cherry tomatoes. Never more
    than pack_quantity.
* shelf_life_days — whole days it stays good from the day it's bought,
    stored the usual way (fridge for fresh dairy, meat and most vegetables,
    pantry for dry goods and cans).
* category — the store section it is found in.
"""
