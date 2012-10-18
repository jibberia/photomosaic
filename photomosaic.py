from __future__ import division
import os
import random
import numpy as np
import scipy
import scipy.misc
import scipy.cluster
import matplotlib
matplotlib.use('Agg')
import Image
import sqlite3
from utils import memo
import color_spaces as cs
from directory_walker import DirectoryWalker

def salient_colors(img, clusters=4, size=100):
    """Group the colors in an image into like clusters, and return a list
    of these colors in order of their abundance in the image."""
    assert img.mode == 'RGB', 'RGB images only!'
    img.thumbnail((size, size))
    imgarr = scipy.misc.fromimage(img)
    imgarr = imgarr.reshape(scipy.product(imgarr.shape[:2]), imgarr.shape[2])
    colors, dist = scipy.cluster.vq.kmeans(imgarr, clusters)
    vecs, dist = scipy.cluster.vq.vq(imgarr, colors)
    counts, bins = scipy.histogram(vecs, len(colors))
    ranking = (-counts).argsort()
    ranked_colors = colors[ranking] # Top 4 colors, in descending order
    total_count = np.sum(counts)
    # If 1 or 2 or 3 colors dominate, leave off the rest.
    prominent_colors = ranked_colors[counts[ranking]/total_count >= 1/(clusters+1)]
    assert prominent_colors.size > 0, "No salient colors!"
    return prominent_colors

def catalog(image_dir, db_name='imagepool.db'):
    """Analyze all the images in image_dir, and store the results in
    a sqlite database at db_name."""
    db = connect(os.path.join(image_dir, db_name))
    try:
        create_tables(db)
        walker = DirectoryWalker(image_dir)
        for filename in walker:
            try:
                img = Image.open(filename)
            except IOError:
                print 'Cannot open %s as an image. Skipping it.' % filename
                continue
            if img.mode != 'RGB':
                print 'RGB images only. Skipping %s.' % filename
                continue
            w, h = img.size
            rgb_colors = salient_colors(img)
            lab_colors = map(cs.rgb2lab, rgb_colors)
            insert(filename, w, h, rgb_colors, lab_colors, db)
        db.commit()
    finally:
        db.close()

def print_db(db):
    "Dump the database to the screen, for debugging."
    c = db.cursor()
    c.execute("SELECT * FROM Images")
    for row in c:
        print row 
    c.execute("SELECT * FROM Colors")
    for row in c:
        print row
    c.close()

def insert(filename, w, h, rgb, lab, db):
    "Register an image in Images and its salient colors in Colors."
    c = db.cursor()
    try:
        c.execute("""INSERT INTO Images (usages, w, h, filename)
                     VALUES (?, ?, ?, ?)""",
                  (0, w, h, filename))
        image_id = c.lastrowid
        for i in xrange(len(rgb)):
            red, green, blue = map(int, rgb[i]) # np.uint8 confuses sqlite3
            L, a, b = lab[i] 
            rank = 1 + i
            c.execute("""INSERT INTO Colors (image_id, rank, 
                         L, a, b, red, green, blue) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                         (image_id, rank, L, a, b, red, green, blue))
    except sqlite3.IntegrityError:
        print "Image %s is already in the table. Skipping it." % filename
    finally:
        c.close()
    
def connect(db_path):
    "Connect to, and if need be create, a sqlite database at db_path."
    try:
        db = sqlite3.connect(db_path)
    except IOError:
        print 'Cannot connect to SQLite database at %s' % db_path
        return
    db.row_factory = sqlite3.Row
    return db

def create_tables(db):
    c = db.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS Images
                 (image_id INTEGER PRIMARY KEY,
                  usages INTEGER,
                  w INTEGER,
                  h INTEGER,
                  filename TEXT UNIQUE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS Colors
                 (color_id INTEGER PRIMARY KEY,
                  image_id INTEGER,
                  rank INTEGER,
                  L REAL,
                  a REAL,
                  b REAL,
                  red INTEGER,
                  green INTEGER,
                  blue INTEGER)""")
    c.close()
    db.commit()

def photomosaic(target_filename, tile_size, db_path):
    """Given the filename of the target image,
    the desired size (w, h) of a tile,
    and the path for a database generated by create_image_pool,
    return a photomosaic image."""
    try:
        target_img = Image.open(target_filename)
    except IOError:
        print "Cannot open %s as an image." % target_filename
        return 1
    tiles = partition_target(target_img, tile_size)
    db = connect(db_path)
    try:
        reset_usage(db)
        for x, row in enumerate(tiles):
            for y, tile in enumerate(row):
                # Replace target tile with a matched tile.
                match = match_colors(tile, db)
                new_tile = make_tile(match, tile_size, vary_size=True)
                tiles[x][y] = new_tile
                print "L_distance {:8.2}  size {}".format(
                    match['L_distance'],
                    str(new_tile.size))
    finally:
        db.close()
    mosaic = assemble_mosaic(tiles, tile_size)
    return mosaic

def reset_usage(db):
    "Before using the image pool, reset image usage count to 0."
    try:
        c = db.cursor()
        c.execute("UPDATE Images SET usages=0")
        c.close()
        db.commit()
    except sqlite3.OperationalError, e:
        print e

def partition_target(img, tile_size):
    "Partition the target image into a 2D list of Images."
    # TODO: Allow to tiles are different sizes. 
    # Merge neighbors that are similar
    # or that inhabit regions of long spatial wavelength.
    width = img.size[0] // tile_size[0]
    height = img.size[1] // tile_size[1]
    tiles = [[None for w in range(width)] for h in range(height)]
    for y in range(height):
        for x in range(width):
            tile = img.crop((x*tile_size[0], 
                             y*tile_size[1],
                             (x + 1)*tile_size[0], 
                             (y + 1)*tile_size[1]))
            tiles[y][x] = tile
    return tiles

def assemble_mosaic(tiles, tile_size, background=(255, 255, 255)):
    "Build the final image."
    # Technically, tile_size could be inferred from a tile,
    # but let's not trust it in this case.
    size = len(tiles[0])*tile_size[0], len(tiles)*tile_size[1]
    mosaic = Image.new('RGB', size, background)
    for y, row in enumerate(tiles):
        for x, tile in enumerate(row):
            pos = tile_position(x, y, tile.size, tile_size, randomize=True)
            mosaic.paste(tile, pos)
    return mosaic # suitable to be saved with imsave

def tile_position(x, y, this_size, generic_size, randomize=True):
    if this_size == generic_size: 
        pos = x*generic_size[0], y*generic_size[1]
    else:
        margin = ((generic_size[0] - this_size[0]) // 2, 
                  (generic_size[1] - this_size[1]) // 2)
        if randomize:
            try:
                # Set left and bottom margins to a random value
                # bound by 0 and twice their original value.
                margin = [random.randrange(2*m) for m in margin]
            except ValueError:
                pass
        pos = x*generic_size[0] + margin[0], y*generic_size[1] + margin[1]
    return pos

def match_colors(tile, db, max_usages=1):
    JND = 2.3 # "just noticeable difference"
    tol = 5*JND
    LIMIT = 1 # Increase from 1 to see runners-up.
    colors = map(cs.rgb2lab, salient_colors(tile))
    if len(colors) > 2:
        # Query images that share two prominent colors.
        L1, a1, b1 = colors[0]
        L2, a2, b2 = colors[1]
        query  =   """SELECT
                   image_id,
                   (a-({a1}))*(a-({a1})) + (b-({b1}))*(b-({b1})) 
                       + (L-({L1}))*(L-({L1})) as E1_sq,
                   (a-({a2}))*(a-({a2})) + (b-({b2}))*(b-({b2})) 
                       + (L-({L2}))*(L-({L2})) as E2_sq,
                   min(L-({L1}), L-({L2})) as L_distance,
                   filename
                   FROM Colors
                   JOIN Images using (image_id)
                   WHERE (E1_sq < {tol}*{tol}
                          OR E2_sq < {tol}*{tol})
                   AND usages < {max_usages}
                   AND rank > 4
                   GROUP BY image_id
                   HAVING COUNT(*) >= 2
                   LIMIT {limit}""".format(a1=a1, b1=b1, L1=L1,
                                           a2=a2, b2=b2, L2=L2,
                                           tol=tol,
                                           max_usages=max_usages,
                                           limit=LIMIT)
    else:
        # Query images that share one prominent color.
        L1, a1, b1 = colors[0]
        query  =   """SELECT
                   image_id,
                   (a-({a1}))*(a-({a1})) + (b-({b1}))*(b-({b1})) 
                       + (L-({L1}))*(L-({L1})) as E1_sq,
                   (L-({L1})) as L_distance,
                   filename
                   FROM Colors
                   JOIN Images using (image_id)
                   WHERE E1_sq < {tol}*{tol}
                   AND usages < {max_usages}
                   AND rank > 3
                   GROUP BY image_id
                   LIMIT {limit}""".format(a1=a1, b1=b1, L1=L1,
                                           tol=tol,
                                           max_usages=max_usages,
                                           limit=LIMIT)
    try:
        c=db.cursor()
        c.execute(query)
        match = c.fetchone()
        if match:
            c.execute("UPDATE Images SET usages=usages+1 WHERE image_id=?", (match['image_id'],))
    finally:
        c.close()
    if match:
        return match
    else:
        # Fall back on old method.
        print "Falling back on Lab proximity method."
        return match_Lab_proximity(tile, db)

def match_Lab_proximity(tile, db, max_usages=1):
    """Query the db for the best match, weighing the color's ab-distance
    in Lab color space, the color's prominence in the image in question
    (its 'rank'), and the image's usage count."""
    target_L, target_a, target_b = map(cs.rgb2lab, salient_colors(tile))[0]
    LIMIT = 1 # Increase to see runners-up.
    # Here, I am working around sqlite's lack of ^ and sqrt operations.
    query = """SELECT
               image_id,
               L, a, b,
               red, green, blue,
               (a-({target_a}))*(a-({target_a})) 
                   + (b-({target_b}))*(b-({target_b})) as ab_distance_sq,
               (L-({target_L})) as L_distance,
               (a-({target_a}))*(a-({target_a})) 
                   + (b-({target_b}))*(b-({target_b})) 
                   + (L-({target_L}))*(L-({target_L})) as E_sq,
               rank,
               usages,
               filename
               FROM Colors
               JOIN Images USING (image_id) 
               WHERE usages <= {max_usages} 
               ORDER BY E_sq ASC
               LIMIT {limit}""".format(
               target_a=target_a, target_b=target_b,
               target_L=target_L, max_usages=max_usages,
               limit=LIMIT)
    try:
        c = db.cursor()
        c.execute(query)
        match = c.fetchone()
        c.execute("""UPDATE Images SET usages=usages+1 WHERE image_id=?""", (match['image_id'],))
    finally:
        c.close()
    return match # a sqlite3.Row object

def make_tile(match, tile_size,
              vary_size=True):
    "Open and resize the matched image."
    raw = Image.open(match['filename'])
    if (match['L_distance'] >= 0 or not vary_size):
        # Match is brighter than target.
        img = crop_to_fit(raw, tile_size)
    else:
        # Match is darker than target.
        # Shrink it to leave white padding.
        img = shrink_to_brighten(raw, tile_size, match['L_distance'])
    return img

def shrink_to_brighten(img, tile_size, L_distance):
    """Return an image smaller than a tile. Its white margins
    will effect lightness. Also, varied tile size looks nice.
    The greater the greater the lightness discrepancy, L_distance,
    the smaller the tile is shrunk."""
    MAX_L_DISTANCE = 100 # the largest possible distance in Lab space
    MIN = 0.5 # not so close small that it's a speck
    MAX = 0.9 # not so close to unity that is looks accidental
    assert L_distance < 0, "Only shrink image when tile is too dark."
    scaling = MAX - (MAX - MIN)*(-L_distance)/MAX_L_DISTANCE
    shrunk_size = [int(scaling*dim) for dim in tile_size]
    img = crop_to_fit(img, shrunk_size) 
    return img 

def crop_to_fit(img, tile_size):
    "Return a copy of img cropped to precisely fill the dimesions tile_size."
    img_w, img_h = img.size
    tile_w, tile_h = tile_size
    img_aspect = img_w // img_h
    tile_aspect = tile_w // tile_h
    if img_aspect > tile_aspect:
        # It's too wide.
        crop_h = img_h
        crop_w = crop_h*tile_aspect
        x_offset = (img_w - crop_w) // 2
        y_offset = 0
    else:
        # It's too tall.
        crop_w = img_w
        crop_h = crop_w // tile_aspect
        x_offset = 0
        y_offset = (img_h - crop_h) // 2
    img = img.crop((x_offset,
                    y_offset,
                    x_offset + crop_w,
                    y_offset + crop_h))
    img = img.resize((tile_w, tile_h), Image.ANTIALIAS)
    return img
    
def color_hex(rgb):
    "Convert [r, g, b] to a HEX value with a leading # character."
    return '#' ''.join(chr(c) for c in color).encode('hex')

def Lab_distance(lab1, lab2):
    """Compute distance in Lab."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    E = np.sqrt((L1 - L2)**2 + (a1 - a2)**2 + (b1 - b2)**2)
    return E
    
def ab_distance(lab1, lab2):
    """Compute distance in the a-b plane, disregarding L."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    return sqrt((a1-a2)**2 + (b1-b2)**2)
