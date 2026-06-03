from PIL import Image, ImageDraw, ImageFont
import os

os.makedirs('extension', exist_ok=True)

for size in [16, 48, 128]:
    img = Image.new('RGB', (size, size), '#1a1a2e')
    draw = ImageDraw.Draw(img)
    # Рисуем белый прямоугольник как логотип
    margin = size // 4
    draw.rectangle([margin, margin//2, size-margin, size-margin//2], fill='white')
    img.save(f'extension/icon{size}.png')

print('Icons created!')
