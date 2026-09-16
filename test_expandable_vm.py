import asyncio
from pyrogram.parser.html import HTML

async def main():
    parser = HTML(None)
    text = (
        "🎵 <b>Shape of You</b>\n"
        "👤 <b>Artist:</b> <i>Ed Sheeran</i>\n\n"
        "📝 <b>Lyrics:</b>\n"
        "<blockquote expandable>"
        "The club isn't the best place to find a lover\n"
        "So the bar is where I go\n"
        "Me and my friends at the table doing shots\n"
        "Drinking fast and then we talk slow"
        "</blockquote>\n\n"
        "<i>Powered by @himayubhai</i>"
    )
    res = await parser.parse(text)
    print("PARSED MESSAGE:\n", res.get("message"))
    print("\nENTITIES COUNT:", len(res.get("entities", [])))
    for ent in res.get("entities", []):
        print("ENTITY:", type(ent), ent)

if __name__ == "__main__":
    asyncio.run(main())
