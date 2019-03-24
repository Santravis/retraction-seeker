#!/usr/bin/python3
from __future__ import print_function
import sys
from string import Template
import math
import json

################################################################################
### Settings ###################################################################
################################################################################
# Note: these can be overriden by creating settings.json file next to the script.
settings = {
    "accel_x": 1000, # max accel mm/s^2
    "accel_y": 1000,
    "accel_z": 500,
    "accel_e": 10000,
    "feed_x": 120, # mm/s
    "feed_y": 120,
    "feed_z": 10,
    "feed_z_m": 600,          # feedrate for z in mm/m
    "feed_e": 120,            # max feedrate for extruder, mm/s
    "temp_bed": 55,           # for PLA this is okay
    "temp_nozzle": 210,       # initial nozzle temperature, will be overriden every Z tile, should be the same as ret_temp_start
    "fan_spd_initial": 0,     # fan speed first layer
    "fan_spd_other": 127,     # fan speed for other layers [0-255]
    "feed_travel": 10*10*60,  # feedrate when traveling mm/min = 8cm*10*60 min
    "feed_print":   6*10*60,      # feedrate when printing mm/min
    "feed_print_outer": 4*10*60,  # feedrate when printing outer wall mm/min
    "feed_print_first": 2*10*60,  # feedrate when printing first layer mm/min
# bed dimensions
    "bed_size_x": 230, # mm
    "bed_size_y": 210, # mm
# nozzle/print characteristics
    "nozzle_diam": 0.4, # mm
    "layer_height": 0.16, # mm
    "width_multiplier": 1.2, # line_width = nozzle_diam * extrusion_multiplier
    "filament_diam": 1.75,
# main settings
    "ret_d_start": 1.0, # mm
    "ret_d_step": 0.25, # mm
    "ret_spd_start": 10, # mm/s
    "ret_spd_step": 2.5, # mm/s
    "ret_temp_start": 210, # Celsius
    "ret_temp_step": -5, # every z tile, we add this to the temperature
    "ret_temp_step_h": int(5/0.16), # no. of layers per temp change - roughly 5 mm here
    "square_size": 3, # mm, size of the side of the printed square pillar
    "max_tile_span": 12, # mm, limits the tile spand for x/y steps for low counts of steps_x/steps_y
    "brim_width": 1, # brim extra width

# X axis tile count
    "steps_x": 20, # X == dist, max = start + steps*step (i.e. 10 steps for default distance: 1.0 + 10*0.25 = 3.5)
# Y axis tile count
    "steps_y": 20, # Y == spd
# Z axis tile count
    "steps_z": 5, # this is 210,205,200,195,190 C
# margins - to not print to the bed's limits [mm]
    "margin_x": 20, # mm
    "margin_y": 20, # mm
# helper stuff, like nozzle prime and similar
    "intro_abl": "",
    "intro_prime": """G1 Y-3.0 F1000.0 ; go outside print area
G92 E0.0
G1 X60.0 E9.0  F1000.0 ; intro line
M73 Q0 S86
M73 P0 R86
G1 X100.0 E12.5  F1000.0 ; intro line
G92 E0.0
"""
};

def comment(s):
    return str(s).replace("\n", "\n;     ")

################################################################################
### Templates ##################################################################
################################################################################

# global settings for the generated g-code
# a modified version of a prusa prologue
gcode_prologue = Template("""
;
; PROLOGUE
; ################
; settings:
$settings
; ################

M73 P0 R86
M73 Q0 S86
M201 X$accel_x Y$accel_y Z$accel_z E$accel_e
M203 X$feed_x Y$feed_y Z$feed_z E$feed_e ; sets maximum feedrates, mm/sec
M205 S0 T0 ; sets the minimum extruding and travel feed rate, mm/sec
M107
M83  ; extruder relative mode
M104 S$temp_nozzle ; set extruder temp
M140 S$temp_bed    ; set bed temp
M190 S$temp_bed    ; wait for bed temp
M109 S$temp_nozzle ; wait for extruder temp
G28 ; home all axes
$intro_abl
$intro_prime
M221 S95
M900 K30; Filament gcode
G21 ; set units to millimeters
G90 ; use absolute coordinates
M83 ; use relative distances for extrusion

""");

gcode_epilogue = Template("""; EPILOGUE
G4 ; wait
M221 S100
M104 S0 ; turn off temperature
M140 S0 ; turn off heatbed
M107 ; turn off fan
G1 Z$park_z ; Move print head up
G1 X0 Y200; home X axis
M84 ; disable motors
""")

# every Z tile we output this prologue (ie. not every layer!)
# by default it sets the current temperature without blocking
# could be commented out to leave the temperature stable - that would enable
# using Z-hop search on Z tile direction
z_tile_prologue = Template("""; -----------------
; Z tile layer $z_tile
; nozzle_temp = $temp_nozzle
M104 $temp_nozzle ; nozzle temp
""");

# this is a fairly standard layer prologue
z_layer_prologue = Template("""
;AFTER_LAYER_CHANGE
;$coord_z
$fan_spd_cmd ; fan speed (or fan off)
G1 Z$coord_z F$feed_z_m ; change the z-coord
""");

# for every tile we generate this prologue
# note: could be use to set settings for firmware retraction
tile_prologue = Template("""; tile x=$tile_x y=$tile_y z=$tile_z
; tile pos x=$tile_origin_x y=$tile_origin_y z=$tile_origin_z
; retraction settings:
;     distance = $deret_d mm
;     speed    = $ret_spd mm/s
; nozzle_temp  = $temp_nozzle
""");

# retraction/derectraction templates. Could be modified to include z-hop
retract_template = Template("""G1 E$ret_d F$ret_feed ; retract
""");
deretract_template = Template("""G1 E$last_ret_d F$ret_feed ; deretract
""");
travel_template = Template("""G1 X$travel_x Y$travel_y F$feed_travel ; travel
""");

################################################################################
### Recalculation functions ####################################################
################################################################################

# these update some of the values in the settings to reflect the current status
def recalculate_z_tile(z):
    settings["temp_nozzle"] = settings["ret_temp_start"] + settings["ret_temp_step"] * settings["z_tile"];
    # this is just informative z_tile origin, it changes in tile steps in z direction (for measuring purposes on Z axis [mm])
    settings["tile_origin_z"] = z * settings["ret_temp_step_h"] * settings["layer_height"];

def recalculate_layer(layer):
    settings["layer"]   = layer;
    settings["coord_z"] = settings["layer_height"] * (layer + 1);
    if (layer == 0):
        fan_spd = settings["fan_spd_initial"];
    else:
        fan_spd = settings["fan_spd_other"];

    if fan_spd == 0:
        settings["fan_spd_cmd"] = "M107";
    else:
        settings["fan_spd_cmd"] = "M106 S%d" % fan_spd;

    settings["fan_spd"] = fan_spd;

# given tile coordinates, recalculate origin of the tile (coord_x, coord_y) and retraction settings
def recalculate_tile_settings(x,y,z):
    # TODO: we could parametrize the coord selection here
    settings["tile_x"] = x;
    settings["tile_y"] = y;
    settings["tile_z"] = z;

    ret_d = settings["ret_d_start"] + settings["ret_d_step"] * x;
    settings["ret_d"] = -ret_d;
    settings["deret_d"] = ret_d;
    settings["ret_spd"] = settings["ret_spd_start"] + settings["ret_spd_step"] * y;
    settings["ret_feed"] = settings["ret_spd"] * 60; # feedrate is in mm/m

    # calculate the origin of the tile
    settings["tile_origin_x"] = settings["tile_x_start"] + x * settings["tile_x_step"];
    settings["tile_origin_y"] = settings["tile_y_start"] + y * settings["tile_y_step"];

# recalculates bed tile positioning, extrusion multiplier, etc
def recalculate_constants():
    # x,y start at margin, end at bed size - 2xmargin
    margin_x = settings["margin_x"];
    margin_y = settings["margin_y"];

    settings["tile_x_start"] = margin_x;
    settings["tile_y_start"] = margin_y;

    span_x = settings["bed_size_x"] - 2 * margin_x;
    span_y = settings["bed_size_y"] - 2 * margin_y;

    # step is span / steps
    tile_x_step = span_x / settings["steps_x"];
    tile_y_step = span_y / settings["steps_y"];

    tile_x_step = min(tile_x_step, settings["max_tile_span"]);
    tile_y_step = min(tile_y_step, settings["max_tile_span"]);

    settings["tile_x_step"] = tile_x_step;
    settings["tile_y_step"] = tile_y_step;

    # insert tuple of all ret_d and ret_spd and temp_nozzle
    settings["ret_d_steps"] = [(settings["ret_d_start"] + settings["ret_d_step"] * (x-1)) for x in range(1, settings["steps_x"])]
    settings["ret_spd_steps"] = [(settings["ret_spd_start"] + settings["ret_spd_step"] * (y-1)) for y in range(1, settings["steps_x"])]
    settings["temp_steps"] = [(settings["ret_temp_start"] + settings["ret_temp_step"] * (z-1)) for z in range(1, settings["steps_z"])]

    # line width is in inverse relationship to layer height, and we should calculate it here
    nozzle_r = settings["nozzle_diam"] / 2;
    nozzle_area = math.pi * (nozzle_r * nozzle_r);
    settings["nozzle_area"] = nozzle_area;

    layer_height = settings["layer_height"];

    settings["line_width"] = settings["nozzle_diam"] * settings["width_multiplier"]

    # extrusion multiplier here is how fast we move e per mm of x/y movement
    # in case the travel speed corresponds to extrusion speed, the area cut of the extruded line will be the same
    #
    width = settings["line_width"];
    # taken from Slic3r flow.cpp
    # Rectangle with semicircles at the ends. ~ h (w - 0.215 h)
    # this is the rough area of the extrusion profile
    mm3_per_mm = layer_height * (width - layer_height * (1. - 0.25 * math.pi));
    settings["mm3_per_mm"] = mm3_per_mm;
    # now based on filament diameter, we can calculate the ratio
    # that means extruded area per area of filament
    filament_r = settings["filament_diam"] / 2;
    filament_area = math.pi * (filament_r * filament_r);
    # this seems close enough to values generated by slic3r. It's slightly more though (7% more in fact)
    # we generate 0.027650062000232345, slic3r uses 0.02565799325936893
    settings["e_per_mm"] = mm3_per_mm / filament_area;

################################################################################
### G-code generators ##########################################################
################################################################################

def generate_retract():
    # refuse to retract in case we're already retracted
    current_ret = settings.get("last_ret_d", 0);
    ret_d       = settings["ret_d"];
    gcode = "";

    if current_ret == 0 and ret_d > 0:
        gcode = retract_template.substitute(settings)
        settings["last_ret_d"] = -ret_d;

    return gcode;

def generate_deretract():
    current_ret = settings.get("last_ret_d", 0);

    gcode = "";

    if (current_ret != 0):
        gcode = deretract_template.substitute(settings);
        settings["last_ret_d"] = 0;

    return gcode;

# generates extruding line from initial to given coordinates
def generate_extrude_line(x, y, feed):
    px = settings["pos_x"];
    py = settings["pos_y"];

    # extrusion multiplier per mm of travel
    e_per_mm = settings["e_per_mm"];

    d_x = x - px;
    d_y = y - py;

    # calculate the travel distance
    travel = math.sqrt(d_x*d_x + d_y*d_y);

    # calculate extrusion distance from travel distance
    e = travel * e_per_mm;

    # update current position
    settings["pos_x"] = x;
    settings["pos_y"] = y;

    return "G1 X%3.6f Y%3.6f E%3.6f F%3.6f\n" % (x,y,e,feed);

def generate_travel(x, y):
    settings["travel_x"] = x;
    settings["travel_y"] = y;
    travel = travel_template.substitute(settings);
    settings["pos_x"] = x;
    settings["pos_y"] = y;
    return travel

def generate_brim():
    origin_x = settings["tile_origin_x"];
    origin_y = settings["tile_origin_y"];
    pad_w    = settings["brim_width"];
    feed     = settings["feed_print_first"];
    square_size = settings["square_size"];
    lw = settings["line_width"];

    x1 = origin_x - pad_w;
    y1 = origin_y - pad_w;
    x2 = origin_x + square_size + pad_w;
    y2 = origin_y + square_size + pad_w;

    # we generate 2 vertical lines per iteration
    lines = int((x2 - x1) / (2*lw));

    # zigzag extrude from
    gcode = generate_travel(x1,y1);

    # de-retract
    gcode += generate_deretract();

    for l in range(0, lines):
        x = x1 + l * lw * 2;
        gcode += generate_extrude_line(x,y2,feed);
        gcode += generate_extrude_line(x + lw, y2,feed);
        gcode += generate_extrude_line(x + lw, y1,feed);
        if (l + 1 < lines):
            gcode += generate_extrude_line(x + 2*lw, y1,feed);

    return gcode

def generate_shape():
    origin_x = settings["tile_origin_x"];
    origin_y = settings["tile_origin_y"];
    feed     = settings["feed_print"];
    feed_o   = settings["feed_print_outer"];

    # first layer contains brim
    if settings["layer"] == 0:
        return generate_brim();

    # first positioning, then the rest is moving extruder too
    gcode = "";

    # is this z tile intro?
    z_intro = settings["z_tile_intro"];
    shrink = 0;
    if (z_intro): # small shrink in shape to serve as marker
        shrink = 0.08;

    # we generate a simple square in rising coordinates
    # size is governed by setting square_size
    square_size = settings["square_size"];

    far_x = origin_x + square_size;
    far_y = origin_y + square_size;

    # line width
    lw = settings["line_width"];

    # TODO: allow overlap between the inner and outer perimeters
    # offsetting to make it internal and shrink on every Z tile layer
    s = 2 * lw + shrink;

    # inner square, if appropriate
    if (square_size - 2*lw >= 2*lw):
        # feedrate to travel speed
        # short travel to origin again
        gcode += generate_travel(origin_x + s, origin_y + s);
        # de-retract
        gcode += generate_deretract();
        # feedrate to print speed
        gcode += generate_extrude_line(far_x - s,    origin_y + s, feed);
        gcode += generate_extrude_line(far_x - s,    far_y - s, feed);
        gcode += generate_extrude_line(origin_x + s, far_y - s, feed);
        gcode += generate_extrude_line(origin_x + s, origin_y + s, feed);

    # TODO: when set, generate infill, etc (complex, so I'm not bothering right now)

    # outer shell now
    s = lw + shrink;

    # travel to far x side of origin
    # (we want to end there so that we don't wipe the nozzle over the print)
    gcode += generate_travel(far_x - s, origin_y + s);

    gcode += generate_deretract();

    # outer shell
    gcode += generate_extrude_line(far_x - s,    far_y - s, feed_o);
    gcode += generate_extrude_line(origin_x + s, far_y - s, feed_o);
    gcode += generate_extrude_line(origin_x + s, origin_y + s, feed_o);

    # note: coasting would be implemented by splitting this line
    # to extrude and travel
    gcode += generate_extrude_line(far_x - s,    origin_y + s, feed_o);
    # note: wipe would be implemented by doing travel in direction of origin_x + s, origin_y + s, with distance being governed by wipe distance
    return gcode;

################################################################################
### Utilities ##################################################################
################################################################################
def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def load_overrides(filename):
    try:
        foverrides = open(filename);
        settings.update(json.load(foverrides));
    except FileNotFoundError:
        # not a problem
        print("; NOTE: no %s config file found - skipping" % filename);
        pass

def sanity_check():
    square_size = settings["square_size"]
    # we have to add 2*brim size to this (brim is on both sides of the tile)
    tile_dim = square_size + 2 * settings["brim_width"]

    if (tile_dim >= settings["tile_x_step"]):
        eprint("square_size is larger than x step width (tile width)")
        exit

    if (tile_dim >= settings["tile_y_step"]):
        eprint("square_size is larger than y step width (tile depth)")
        exit

def print_retraction_map():
    # print a helpful guide for retraction tracking
    print("; ==== retraction map ====");
    print("; ");
    print(";  Y (retr. speed)");
    print(";  ^ ");
    print("; 0,0 > X (retr. distance)");
    print("; ");
    for y in reversed(range(0, settings["steps_y"])):
        spd = settings["ret_spd_start"] + settings["ret_spd_step"] * y;
        line = "; [%2d] %2.2f mm/s :  [ 1]" % (y + 1, spd);
        for x in range(1, settings["steps_x"]):
            dist = settings["ret_d_start"] + settings["ret_d_step"] * (x-1);
            line += " .. %3.1f mm .. [%2d]" % (dist, x + 1);
        print(line);
    print("; ");
    print("; Z: (print temp.)");
    for z in reversed(range(0, settings["steps_z"])):
        temp = settings["ret_temp_start"] + settings["ret_temp_step"] * z;
        height = z * settings["ret_temp_step_h"] * settings["layer_height"];
        print("; [%2d] - %3.2f mm - %3.2f C" % (z + 1, height, temp));

    print("; ========================");
    print("");

################################################################################
### Main code ##################################################################
################################################################################
print("; #############################################");
print("; generated by retraction-seeker.py");
print("; http://github.com/volca02/retraction-seeker/");
print("; #############################################");
print("; ");

# override settings by reading machine.json followed by settings.json
load_overrides("machine.json");
load_overrides("settings.json");

# this calculates helper constants so that we know where to place the pillars
recalculate_constants();

# sanity check
sanity_check();

# after all the related constants were calculated, we generate the string containing all settings
# insert a text representation of the settings into the settings as well as a commented multiline string...
s = "\n".join([(";   %s = %s" % (i[0], comment(i[1]))) for i in settings.items()]);
settings["settings"] = s;

# we retract in the next statement, so we prepare for zero tile
recalculate_tile_settings(0,0,0);

print_retraction_map();

# generate the prologue
print(gcode_prologue.substitute(settings));

# retract since we'll be traveling to first tile and de-retracting
print(generate_retract())

# generate the retraction pattern
for z_tile in range(0, settings["steps_z"]):
    settings["z_tile"] = z_tile;
    settings["z_tile_intro"] = True; # can be used to mark the layers where Z tile changed

    # calculate the current temp
    recalculate_z_tile(z_tile);
    print(z_tile_prologue.substitute(settings));

    # n layers of the current Z tile
    z_tile_layers = settings["ret_temp_step_h"];
    for z_layer in range(0, z_tile_layers):
        # recalculate the z coord
        recalculate_layer(z_tile_layers * z_tile + z_layer);
        print(z_layer_prologue.substitute(settings));

        # Y tiles
        for y_tile in range(0, settings["steps_y"]):
            for x_tile in range(0, settings["steps_x"]):
                # origin for the current tile is recalculated
                recalculate_tile_settings(x_tile,y_tile,z_tile);
                # intro G-code for the tile
                print(tile_prologue.substitute(settings));
                # generate the G-code for the tile - contains deretraction as appropriate
                print(generate_shape())
                # generate the retraction code
                print(generate_retract())

        # not a z intro any more
        settings["z_tile_intro"] = False;

# TODO: this could crash the z if it is too high
# park 5 mm above the print
settings["park_z"] = settings["coord_z"] + 5

print(gcode_epilogue.substitute(settings));
