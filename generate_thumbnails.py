import json
import os
from PIL import Image, ImageDraw

def hex_to_rgb(hex_code):
    hex_code = hex_code.lstrip('#')
    return tuple(int(hex_code[i:i+2], 16) for i in (0, 2, 4))

def create_swatch(color_data, size=(200, 200)):
    base_color = hex_to_rgb(color_data['code'])
    img = Image.new('RGB', size, base_color)
    draw = ImageDraw.Draw(img)
    
    if color_data.get('isSplatter'):
        splatter_colors = [hex_to_rgb(c) for c in color_data['splatterColors']]
        import random
        random.seed(hash(color_data['name']) % 10000) # consistent seed per color
        # Draw scattered circles (dots)
        for _ in range(80):
            r = random.randint(4, 12)
            x = random.randint(-r, size[0]+r)
            y = random.randint(-r, size[1]+r)
            sc = random.choice(splatter_colors)
            draw.ellipse((x-r, y-r, x+r, y+r), fill=sc)
            
    elif color_data.get('isSmearedSplatter'):
        splatter_colors = [hex_to_rgb(c) for c in color_data['splatterColors']]
        import random
        random.seed(hash(color_data['name']) % 10000)
        
        # Draw some thick diagonal strokes
        for _ in range(8):
            sc = random.choice(splatter_colors)
            x0 = random.randint(-size[0], size[0])
            y0 = -100
            x1 = x0 + random.randint(150, 300)
            y1 = size[1] + 100
            width = random.randint(20, 60)
            draw.line((x0, y0, x1, y1), fill=sc, width=width)
            
        # Draw some scattered dots too
        for _ in range(40):
            r = random.randint(6, 20)
            x = random.randint(-r, size[0]+r)
            y = random.randint(-r, size[1]+r)
            sc = random.choice(splatter_colors)
            draw.ellipse((x-r, y-r, x+r, y+r), fill=sc)
                
    return img

def main():
    with open('bCNC/skuData.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    out_dir = os.path.join('test_codes', 'sku_maker', 'thumbnails')
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
        
    for color in data.get('colors', []):
        img = create_swatch(color, size=(200, 200))
        # clean filename
        filename = color['name'].replace(' ', '_').replace('+', 'and').lower() + '.png'
        filepath = os.path.join(out_dir, filename)
        img.save(filepath)
        print(f"Generated {filepath}")

if __name__ == '__main__':
    main()
