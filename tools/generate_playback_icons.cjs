// Offline, reproducible PNG generation from the vendored official SVGs.
// Development dependency only: npm install --no-save sharp@0.35.4
const fs = require('node:fs');
const path = require('node:path');
const sharp = require('sharp');
const root = path.resolve(__dirname, '../assets/bootstrap-icons');
async function main() {
  const files = fs.readdirSync(path.join(root, 'svg')).filter(x => x.endsWith('.svg')).sort();
  const composite = [], index = {};
  let top = 0;
  for (const [state, color] of Object.entries({normal:'#dce6f3', disabled:'#758399'})) {
    for (const size of [20, 25, 30, 40]) {
      for (const [column, filename] of files.entries()) {
        const svg = fs.readFileSync(path.join(root, 'svg', filename), 'utf8');
        const input = await sharp(Buffer.from(svg.replaceAll('currentColor', color))).resize(size, size).png().toBuffer();
        const left = column * 44;
        composite.push({input, top, left});
        index[`${filename.replace('.svg', '')}/${state}/${size}`] = [left, top, size];
      }
      top += size + 4;
    }
  }
  await sharp({create:{width:files.length*44, height:top, channels:4, background:'#00000000'}})
    .composite(composite).png().toFile(path.join(root, 'sprite.png'));
  fs.writeFileSync(path.join(root, 'sprite.json'), JSON.stringify(index, null, 2)+'\n');
}
main().catch(() => { process.stderr.write('Icon generation failed\n'); process.exitCode = 1; });
