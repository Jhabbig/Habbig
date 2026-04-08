"""Comprehensive infrastructure data for the World State Dashboard.

Contains detailed routing data for submarine cables, oil/gas pipelines,
and mineral deposit locations with area extents.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# SUBMARINE CABLES
# Waypoints are [longitude, latitude] arrays tracing actual ocean-floor routes.
# ═══════════════════════════════════════════════════════════════════════════════

UNDERSEA_CABLES = [
    # ────────────────────────────────────────────────────────────────────────
    # Trans-Atlantic: US East Coast <-> Europe
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "TAT-14",
        "capacity": "3.2 Tbps",
        "length_km": 15428,
        "rfs": 2001,
        "owners": "Deutsche Telekom/AT&T/others",
        "waypoints": [
            [-73.97, 40.57],    # Tuckerton, New Jersey
            [-68.0, 41.0],      # off continental shelf
            [-55.0, 44.0],      # Grand Banks approach
            [-40.0, 48.0],      # mid-Atlantic
            [-25.0, 50.0],      # mid-Atlantic ridge area
            [-15.0, 51.0],      # approaching European shelf
            [-10.0, 51.5],      # Celtic Sea
            [-5.2, 50.83],      # Bude, UK (branch)
            [-4.0, 52.0],       # Irish Sea approach
            [-1.0, 53.0],       # North Sea entry
            [3.0, 54.0],        # North Sea
            [7.2, 53.7],        # Norden, Germany
        ],
    },
    {
        "name": "FLAG Atlantic-1 (FA-1)",
        "capacity": "10 Tbps",
        "length_km": 6300,
        "rfs": 2001,
        "owners": "Reliance Globalcom",
        "waypoints": [
            [-73.97, 40.57],    # New York area
            [-68.0, 40.0],      # off continental shelf
            [-55.0, 42.0],      # mid-ocean
            [-40.0, 44.0],      # mid-Atlantic
            [-25.0, 47.0],      # mid-Atlantic ridge
            [-15.0, 49.0],      # approaching Europe
            [-10.0, 50.0],      # Western Approaches
            [-5.0, 50.3],       # English Channel approach
            [1.43, 51.35],      # Whitstable, UK
        ],
    },
    {
        "name": "MAREA",
        "capacity": "200 Tbps",
        "length_km": 6600,
        "rfs": 2018,
        "owners": "Microsoft/Meta/Telxius",
        "waypoints": [
            [-74.45, 39.29],    # Virginia Beach
            [-65.0, 40.5],      # off continental shelf
            [-50.0, 42.0],      # mid-Atlantic
            [-35.0, 43.0],      # mid-Atlantic
            [-20.0, 44.0],      # eastern mid-Atlantic
            [-10.0, 44.0],      # Bay of Biscay approach
            [-3.21, 43.37],     # Bilbao, Spain
        ],
    },
    {
        "name": "Dunant",
        "capacity": "250 Tbps",
        "length_km": 6400,
        "rfs": 2020,
        "owners": "Google",
        "waypoints": [
            [-74.45, 39.29],    # Virginia Beach
            [-65.0, 40.0],      # off shelf
            [-50.0, 42.0],      # mid-ocean
            [-35.0, 44.0],      # mid-Atlantic
            [-20.0, 45.5],      # eastern Atlantic
            [-10.0, 46.5],      # Bay of Biscay
            [-5.0, 47.0],       # Brittany approach
            [-2.52, 47.29],     # Saint-Hilaire-de-Riez, France
        ],
    },
    {
        "name": "AC-1 (Atlantic Crossing-1)",
        "capacity": "40 Gbps",
        "length_km": 14000,
        "rfs": 1998,
        "owners": "Telia/Level 3",
        "waypoints": [
            [-72.85, 40.82],    # Brookhaven, NY
            [-65.0, 41.5],      # off shelf
            [-50.0, 44.0],      # mid-ocean
            [-35.0, 47.0],      # mid-Atlantic
            [-20.0, 49.0],      # eastern mid-Atlantic
            [-10.0, 50.0],      # Western Approaches
            [-5.0, 50.2],       # Cornwall approach
            [-4.55, 50.83],     # Bude, Cornwall, UK
        ],
    },
    {
        "name": "AC-2 (Atlantic Crossing-2)",
        "capacity": "640 Gbps",
        "length_km": 6400,
        "rfs": 2000,
        "owners": "Telia/Level 3",
        "waypoints": [
            [-74.0, 40.5],      # New Jersey coast
            [-65.0, 41.0],      # off shelf
            [-50.0, 43.5],      # mid-ocean
            [-35.0, 46.0],      # mid-Atlantic
            [-20.0, 48.5],      # eastern Atlantic
            [-10.0, 50.0],      # Western Approaches
            [-5.0, 50.2],       # Cornwall approach
            [-4.55, 50.83],     # Bude, Cornwall, UK
        ],
    },
    {
        "name": "Apollo",
        "capacity": "3.2 Tbps",
        "length_km": 13000,
        "rfs": 2003,
        "owners": "Vodafone/GTT",
        "waypoints": [
            [-73.7, 40.6],      # New York area
            [-65.0, 41.0],      # off shelf
            [-50.0, 43.0],      # mid-ocean
            [-35.0, 46.0],      # mid-Atlantic
            [-20.0, 48.5],      # eastern Atlantic
            [-12.0, 49.5],      # Western Approaches
            [-6.0, 50.0],       # Cornwall approach
            [-4.55, 50.83],     # Bude, Cornwall, UK
        ],
    },
    {
        "name": "Hibernia Express",
        "capacity": "53 Tbps",
        "length_km": 4600,
        "rfs": 2015,
        "owners": "GTT/Hibernia Networks",
        "waypoints": [
            [-63.57, 44.65],    # Halifax, Nova Scotia
            [-55.0, 47.0],      # Grand Banks
            [-40.0, 49.5],      # mid-Atlantic
            [-25.0, 51.0],      # eastern mid-Atlantic
            [-15.0, 51.5],      # approaching Ireland
            [-9.74, 51.84],     # Cork, Ireland
        ],
    },
    {
        "name": "AEConnect-1",
        "capacity": "52 Tbps",
        "length_km": 5500,
        "rfs": 2016,
        "owners": "Aqua Comms",
        "waypoints": [
            [-73.7, 40.6],      # New York area
            [-65.0, 41.5],      # off shelf
            [-50.0, 45.0],      # mid-ocean
            [-35.0, 48.0],      # mid-Atlantic
            [-20.0, 50.5],      # eastern Atlantic
            [-12.0, 51.5],      # approaching Ireland
            [-9.74, 51.84],     # Cork area, Ireland
        ],
    },
    {
        "name": "Grace Hopper",
        "capacity": "340 Tbps",
        "length_km": 6300,
        "rfs": 2022,
        "owners": "Google",
        "waypoints": [
            [-73.7, 40.6],      # New York area
            [-65.0, 41.0],      # off shelf
            [-50.0, 43.0],      # mid-ocean
            [-35.0, 44.5],      # mid-Atlantic
            [-20.0, 46.0],      # eastern Atlantic
            [-10.0, 48.0],      # Bay of Biscay
            [-3.21, 43.37],     # Bilbao, Spain (branch)
            [-5.0, 50.2],       # Cornwall approach (branch)
            [-4.55, 50.83],     # Bude, UK (branch)
        ],
    },
    {
        "name": "Amitie",
        "capacity": "400 Tbps",
        "length_km": 6800,
        "rfs": 2022,
        "owners": "Google/Meta/Lumen",
        "waypoints": [
            [-74.45, 39.29],    # Virginia Beach
            [-65.0, 40.5],      # off shelf
            [-50.0, 42.5],      # mid-ocean
            [-35.0, 44.5],      # mid-Atlantic
            [-20.0, 46.0],      # eastern Atlantic
            [-10.0, 47.5],      # Bay of Biscay
            [-5.0, 48.0],       # Brittany approach
            [-3.0, 48.6],       # Le Porge, France (branch)
            [-5.5, 50.5],       # Cornwall approach (branch)
            [-4.55, 50.83],     # Bude, UK (branch)
        ],
    },
    {
        "name": "Havfrue/AEC-2",
        "capacity": "108 Tbps",
        "length_km": 7800,
        "rfs": 2020,
        "owners": "Aqua Comms/Google/Facebook",
        "waypoints": [
            [-73.7, 40.6],      # New York area
            [-65.0, 42.0],      # off shelf
            [-50.0, 46.0],      # mid-ocean, northerly route
            [-35.0, 50.0],      # mid-Atlantic
            [-20.0, 53.0],      # eastern Atlantic
            [-10.0, 55.0],      # north of Scotland
            [-2.0, 57.0],       # North Sea
            [3.0, 57.0],        # central North Sea
            [8.0, 56.5],        # approaching Denmark
            [9.87, 57.05],      # Blaabjerg, Denmark
        ],
    },
    {
        "name": "GTT Atlantic",
        "capacity": "13 Tbps",
        "length_km": 6500,
        "rfs": 2016,
        "owners": "GTT Communications",
        "waypoints": [
            [-73.7, 40.6],      # New York area
            [-65.0, 41.0],      # off shelf
            [-50.0, 43.5],      # mid-ocean
            [-35.0, 46.0],      # mid-Atlantic
            [-20.0, 48.0],      # eastern Atlantic
            [-10.0, 50.0],      # Western Approaches
            [-5.0, 50.2],       # Cornwall approach
            [-4.55, 50.83],     # Bude, Cornwall, UK
        ],
    },
    {
        "name": "Yellow/AC-3",
        "capacity": "400 Tbps",
        "length_km": 7100,
        "rfs": 2025,
        "owners": "Aqua Comms/Meta",
        "waypoints": [
            [-73.7, 40.6],      # New York area
            [-65.0, 41.0],      # off shelf
            [-50.0, 43.5],      # mid-ocean
            [-35.0, 45.5],      # mid-Atlantic
            [-20.0, 47.5],      # eastern Atlantic
            [-10.0, 48.5],      # Bay of Biscay
            [-5.0, 48.8],       # Brittany approach
            [-2.0, 48.5],       # France landing (branch)
            [-5.5, 50.5],       # Cornwall approach (branch)
            [-4.55, 50.83],     # Bude, UK (branch)
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Trans-Atlantic: South
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "EllaLink",
        "capacity": "72 Tbps",
        "length_km": 6000,
        "rfs": 2021,
        "owners": "EllaLink Group",
        "waypoints": [
            [-38.52, -3.72],    # Fortaleza, Brazil
            [-35.0, -2.0],      # off Brazilian shelf
            [-30.0, 2.0],       # equatorial Atlantic
            [-25.0, 8.0],       # mid-ocean
            [-20.0, 15.0],      # eastern Atlantic
            [-18.0, 22.0],      # off Western Sahara
            [-15.0, 28.8],      # Canary Islands area
            [-12.0, 33.0],      # off Morocco
            [-9.14, 38.72],     # Lisbon (Sines), Portugal
        ],
    },
    {
        "name": "SACS (South Atlantic Cable System)",
        "capacity": "100 Tbps",
        "length_km": 6200,
        "rfs": 2018,
        "owners": "Angola Cables",
        "waypoints": [
            [13.23, -8.84],     # Luanda, Angola
            [8.0, -10.0],       # off Angola coast
            [0.0, -12.0],       # mid-South Atlantic
            [-10.0, -10.0],     # mid-ocean
            [-20.0, -8.0],      # approaching Brazil
            [-30.0, -5.0],      # off northeastern Brazil
            [-38.52, -3.72],    # Fortaleza, Brazil
        ],
    },
    {
        "name": "Monet",
        "capacity": "60 Tbps",
        "length_km": 10500,
        "rfs": 2017,
        "owners": "Google/Algar/Antel",
        "waypoints": [
            [-80.08, 26.36],    # Boca Raton, Florida
            [-77.0, 24.0],      # Bahamas passage
            [-70.0, 20.0],      # Caribbean Sea
            [-60.0, 14.0],      # Lesser Antilles
            [-50.0, 5.0],       # equatorial approach
            [-42.0, -2.0],      # off Brazilian coast
            [-38.52, -3.72],    # Fortaleza, Brazil
        ],
    },
    {
        "name": "BRUSA",
        "capacity": "138 Tbps",
        "length_km": 10700,
        "rfs": 2018,
        "owners": "Telxius (Telefonica)",
        "waypoints": [
            [-74.45, 39.29],    # Virginia Beach
            [-70.0, 36.0],      # off Virginia coast
            [-60.0, 28.0],      # Sargasso Sea
            [-55.0, 20.0],      # Caribbean approach
            [-50.0, 10.0],      # near Trinidad
            [-42.0, 0.0],       # equatorial
            [-38.52, -3.72],    # Fortaleza, Brazil
        ],
    },
    {
        "name": "Seabras-1",
        "capacity": "72 Tbps",
        "length_km": 10600,
        "rfs": 2017,
        "owners": "Seaborn Networks",
        "waypoints": [
            [-73.7, 40.6],      # New York area
            [-70.0, 37.0],      # off mid-Atlantic coast
            [-60.0, 28.0],      # Bermuda area
            [-50.0, 18.0],      # Caribbean
            [-42.0, 5.0],       # equatorial approach
            [-38.0, -2.0],      # off Brazil
            [-38.52, -3.72],    # Fortaleza, Brazil (waypoint)
            [-38.0, -10.0],     # along Brazilian coast
            [-39.0, -15.0],     # Bahia
            [-43.17, -22.91],   # Rio de Janeiro coast
            [-46.33, -23.95],   # Sao Paulo coast (Praia Grande)
        ],
    },
    {
        "name": "SAEx-1 (South Atlantic Express)",
        "capacity": "12.8 Tbps",
        "length_km": 10000,
        "rfs": 2020,
        "owners": "SAEx International",
        "waypoints": [
            [18.42, -33.92],    # Cape Town, South Africa
            [10.0, -30.0],      # off South Africa west coast
            [0.0, -25.0],       # mid-South Atlantic
            [-15.0, -18.0],     # mid-ocean
            [-25.0, -12.0],     # approaching Brazil
            [-35.0, -6.0],      # off northeast Brazil
            [-38.52, -3.72],    # Fortaleza, Brazil
            [-42.0, -2.0],      # along Brazilian coast
            [-55.0, 10.0],      # Caribbean approach
            [-65.0, 20.0],      # Caribbean Sea
            [-74.45, 39.29],    # Virginia Beach, US
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Caribbean / Americas
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Americas-II",
        "capacity": "1.92 Tbps",
        "length_km": 8400,
        "rfs": 2000,
        "owners": "AT&T/Sprint/others",
        "waypoints": [
            [-80.13, 26.0],     # Hollywood, Florida
            [-79.0, 23.0],      # Bahamas
            [-76.0, 19.5],      # off Jamaica
            [-69.0, 18.5],      # Dominican Republic
            [-67.0, 18.4],      # Puerto Rico
            [-64.0, 18.3],      # US Virgin Islands
            [-62.0, 17.0],      # Leeward Islands
            [-61.5, 15.4],      # Martinique
            [-61.0, 13.1],      # Barbados approach
            [-60.0, 10.5],      # Trinidad
            [-52.0, 5.0],       # off Guyana
            [-38.52, -3.72],    # Fortaleza, Brazil
        ],
    },
    {
        "name": "ARCOS (Americas Region Caribbean Optical-ring System)",
        "capacity": "1.92 Tbps",
        "length_km": 8600,
        "rfs": 2001,
        "owners": "Telia Carrier",
        "waypoints": [
            [-80.13, 25.77],    # Miami, Florida
            [-81.0, 23.5],      # off Cuba west
            [-86.95, 21.17],    # Cancun, Mexico
            [-87.47, 15.78],    # Honduras coast
            [-85.0, 12.5],      # Nicaragua coast
            [-83.0, 10.0],      # Costa Rica/Panama
            [-79.5, 9.0],       # Colon, Panama
            [-76.5, 9.5],       # Cartagena, Colombia
            [-74.0, 18.5],      # off Jamaica south
            [-76.8, 17.97],     # Kingston, Jamaica
            [-72.0, 18.5],      # Haiti
            [-69.9, 18.5],      # Dominican Republic
            [-66.5, 18.5],      # Puerto Rico
            [-80.13, 25.77],    # back to Miami
        ],
    },
    {
        "name": "Maya-1",
        "capacity": "22.4 Tbps",
        "length_km": 4400,
        "rfs": 2013,
        "owners": "Telxius (Telefonica)",
        "waypoints": [
            [-80.13, 25.77],    # Miami, Florida
            [-82.0, 23.0],      # off Cuba west
            [-86.95, 21.17],    # Cancun, Mexico
            [-88.0, 18.5],      # Belize area
            [-88.5, 15.5],      # Honduras
            [-86.5, 12.1],      # Nicaragua coast
            [-84.0, 10.0],      # Costa Rica coast
            [-82.0, 9.5],       # Panama
            [-79.9, 9.35],      # Colon, Panama
        ],
    },
    {
        "name": "Deep Blue Cable",
        "capacity": "24 Tbps",
        "length_km": 2400,
        "rfs": 2020,
        "owners": "Digicel",
        "waypoints": [
            [-80.13, 25.77],    # Miami, Florida
            [-79.0, 23.0],      # Bahamas
            [-77.0, 20.0],      # off Cuba east
            [-72.0, 19.5],      # off Haiti
            [-69.0, 18.5],      # Dominican Republic
            [-67.0, 18.4],      # Puerto Rico
            [-64.0, 18.3],      # Virgin Islands
            [-62.0, 17.0],      # Leeward Islands
            [-61.0, 14.0],      # Windward Islands
        ],
    },
    {
        "name": "AMX-1",
        "capacity": "80 Tbps",
        "length_km": 17500,
        "rfs": 2015,
        "owners": "America Movil",
        "waypoints": [
            [-80.13, 25.77],    # Miami, Florida
            [-79.0, 23.0],      # Bahamas
            [-76.0, 19.5],      # off Jamaica
            [-74.0, 12.5],      # off Colombia
            [-79.0, 9.0],       # Panama
            [-86.0, 16.0],      # Honduras
            [-87.5, 21.0],      # Cancun area
            [-90.0, 19.5],      # Yucatan
            [-94.5, 19.2],      # Veracruz, Mexico
            [-80.13, 25.77],    # back to Miami
            [-66.0, 18.5],      # Puerto Rico branch
            [-63.0, 10.5],      # Trinidad branch
            [-46.33, -23.95],   # Sao Paulo coast, Brazil
        ],
    },
    {
        "name": "GlobeNet",
        "capacity": "11.4 Tbps",
        "length_km": 23000,
        "rfs": 2000,
        "owners": "Lumen Technologies",
        "waypoints": [
            [-80.13, 25.77],    # Miami, Florida
            [-79.0, 23.0],      # Bahamas
            [-70.0, 18.5],      # off Hispaniola
            [-63.0, 10.5],      # Trinidad
            [-52.0, 5.0],       # off Guyana
            [-38.52, -3.72],    # Fortaleza, Brazil
            [-38.5, -13.0],     # Salvador, Brazil
            [-43.17, -22.91],   # Rio de Janeiro coast
            [-46.33, -23.95],   # Santos, Brazil
            [-48.5, -27.6],     # Florianopolis
        ],
    },
    {
        "name": "SAm-1 (South American Crossing)",
        "capacity": "1.92 Tbps",
        "length_km": 25000,
        "rfs": 2000,
        "owners": "Lumen Technologies",
        "waypoints": [
            [-80.13, 25.77],    # Miami, Florida
            [-80.0, 22.0],      # off Cuba
            [-76.0, 12.0],      # off Colombia
            [-79.0, 9.0],       # Panama area
            [-80.0, 5.0],       # off Ecuador
            [-81.0, -2.0],      # off Peru
            [-77.0, -12.0],     # Lima, Peru approach
            [-76.0, -16.0],     # southern Peru coast
            [-71.3, -33.4],     # Valparaiso, Chile
            [-70.0, -40.0],     # southern Chile coast
            [-67.5, -45.0],     # Patagonian coast (Argentina side)
            [-58.0, -34.6],     # Buenos Aires, Argentina
            [-48.5, -27.6],     # Florianopolis, Brazil
            [-46.33, -23.95],   # Santos, Brazil
            [-43.17, -22.91],   # Rio de Janeiro
            [-38.52, -3.72],    # Fortaleza, Brazil
        ],
    },
    {
        "name": "Curie",
        "capacity": "72 Tbps",
        "length_km": 10500,
        "rfs": 2019,
        "owners": "Google",
        "waypoints": [
            [-118.19, 33.77],   # Los Angeles, CA
            [-118.5, 32.0],     # off southern California
            [-115.0, 28.0],     # off Baja California
            [-108.0, 22.0],     # off western Mexico
            [-100.0, 15.0],     # off Central America
            [-92.0, 8.0],       # off Panama/Colombia
            [-84.0, 0.0],       # equatorial Pacific
            [-78.0, -5.0],      # off northern Peru
            [-74.0, -12.0],     # off Lima
            [-71.6, -33.0],     # Valparaiso, Chile
        ],
    },
    {
        "name": "Firmina",
        "capacity": "192 Tbps",
        "length_km": 13800,
        "rfs": 2023,
        "owners": "Google",
        "waypoints": [
            [-74.45, 39.29],    # Virginia Beach
            [-70.0, 35.0],      # off US East Coast
            [-60.0, 25.0],      # Sargasso Sea
            [-50.0, 12.0],      # Caribbean approach
            [-42.0, 0.0],       # equatorial Atlantic
            [-38.52, -3.72],    # Fortaleza, Brazil
            [-38.5, -13.0],     # Salvador, Brazil branch
            [-43.0, -22.9],     # Rio de Janeiro branch
            [-50.0, -28.0],     # south Brazil coast
            [-55.0, -34.0],     # off Uruguay
            [-56.17, -34.91],   # Montevideo, Uruguay (branch)
            [-56.0, -36.0],     # off River Plate
            [-57.0, -38.0],     # off Argentina coast
            [-57.54, -38.72],   # Las Toninas, Argentina
        ],
    },
    {
        "name": "Malbec",
        "capacity": "138 Tbps",
        "length_km": 2600,
        "rfs": 2023,
        "owners": "Telxius/GlobeNet/Antel",
        "waypoints": [
            [-57.54, -38.72],   # Las Toninas, Argentina
            [-56.0, -36.0],     # off River Plate
            [-54.0, -34.0],     # off Uruguay
            [-50.0, -30.0],     # south Brazil coast
            [-46.33, -23.95],   # Santos / Sao Paulo coast, Brazil
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Trans-Pacific
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "JUPITER",
        "capacity": "60 Tbps",
        "length_km": 14000,
        "rfs": 2020,
        "owners": "Google/Facebook/PLDT/others",
        "waypoints": [
            [139.69, 35.68],    # Chiba, Japan
            [142.0, 35.0],      # off Japanese coast
            [148.0, 32.0],      # open Pacific
            [155.0, 28.0],      # mid-Pacific
            [144.8, 13.5],      # Guam
            [155.0, 20.0],      # mid-Pacific
            [170.0, 28.0],      # central Pacific
            [-180.0, 35.0],     # near Date Line
            [-170.0, 38.0],     # eastern Pacific
            [-155.0, 42.0],     # mid-Pacific
            [-140.0, 44.0],     # eastern Pacific
            [-130.0, 46.0],     # approaching West Coast
            [-122.33, 47.61],   # Seattle / Pacific City
        ],
    },
    {
        "name": "FASTER",
        "capacity": "60 Tbps",
        "length_km": 11600,
        "rfs": 2016,
        "owners": "Google/KDDI/China Telecom/SingTel",
        "waypoints": [
            [139.69, 35.68],    # Chiba, Japan
            [142.0, 36.0],      # off Japan east coast
            [155.0, 38.0],      # open Pacific
            [170.0, 40.0],      # mid-Pacific
            [-180.0, 42.0],     # Date Line
            [-165.0, 43.5],     # eastern Pacific
            [-150.0, 44.0],     # mid-Pacific
            [-135.0, 44.5],     # approaching US
            [-124.12, 43.96],   # Bandon, Oregon
        ],
    },
    {
        "name": "Unity (EAC-Pacific)",
        "capacity": "7.68 Tbps",
        "length_km": 10000,
        "rfs": 2010,
        "owners": "Google/KDDI",
        "waypoints": [
            [139.69, 35.68],    # Chiba, Japan
            [142.0, 34.0],      # off Japan
            [155.0, 32.0],      # Pacific
            [170.0, 30.0],      # mid-Pacific
            [-180.0, 30.0],     # Date Line
            [-165.0, 30.5],     # eastern Pacific
            [-150.0, 31.0],     # mid-Pacific
            [-135.0, 32.0],     # approaching US
            [-120.0, 33.0],     # off California
            [-118.19, 33.77],   # Los Angeles area
        ],
    },
    {
        "name": "TPE (Trans-Pacific Express)",
        "capacity": "5.12 Tbps",
        "length_km": 18000,
        "rfs": 2008,
        "owners": "Verizon/China Telecom/others",
        "waypoints": [
            [121.5, 25.0],      # Taiwan (Tanshui)
            [120.3, 23.0],      # off Taiwan south
            [117.0, 20.0],      # South China Sea
            [114.17, 22.25],    # Hong Kong area
            [118.0, 24.5],      # off Fujian
            [124.0, 30.0],      # East China Sea
            [140.0, 35.0],      # off Japan
            [155.0, 38.0],      # Pacific
            [170.0, 40.0],      # mid-Pacific
            [-180.0, 42.0],     # Date Line
            [-165.0, 43.0],     # eastern Pacific
            [-150.0, 43.5],     # mid-Pacific
            [-135.0, 44.0],     # approaching US
            [-124.12, 43.96],   # Oregon coast
        ],
    },
    {
        "name": "NCP (New Cross Pacific)",
        "capacity": "80 Tbps",
        "length_km": 13600,
        "rfs": 2018,
        "owners": "Microsoft/others",
        "waypoints": [
            [139.69, 35.68],    # Chiba, Japan
            [142.0, 36.0],      # off Japan
            [155.0, 38.0],      # Pacific
            [170.0, 40.0],      # mid-Pacific
            [-180.0, 42.0],     # Date Line
            [-165.0, 43.0],     # eastern Pacific
            [-150.0, 43.5],     # mid-Pacific
            [-135.0, 44.0],     # approaching US
            [-124.12, 43.96],   # Oregon coast (Hillsboro)
        ],
    },
    {
        "name": "PC-1 (Pacific Crossing-1)",
        "capacity": "640 Gbps",
        "length_km": 21000,
        "rfs": 1999,
        "owners": "NTT/others",
        "waypoints": [
            [139.69, 35.68],    # Japan
            [145.0, 35.0],      # off Japan east
            [155.0, 37.0],      # open Pacific
            [170.0, 39.0],      # mid-Pacific
            [-175.0, 41.0],     # central Pacific
            [-160.0, 42.5],     # east-central Pacific
            [-145.0, 43.5],     # approaching US
            [-130.0, 44.0],     # off West Coast
            [-124.12, 43.96],   # Oregon coast
        ],
    },
    {
        "name": "Southern Cross NEXT",
        "capacity": "72 Tbps",
        "length_km": 13000,
        "rfs": 2022,
        "owners": "Southern Cross Cables/Spark NZ",
        "waypoints": [
            [151.21, -33.87],   # Sydney, Australia
            [155.0, -32.0],     # off Sydney
            [165.0, -30.0],     # Tasman Sea
            [174.76, -36.85],   # Auckland, NZ (branch)
            [180.0, -30.0],     # Fiji area
            [-175.0, -20.0],    # Samoa/Tonga area
            [-165.0, -10.0],    # central Pacific
            [-158.0, 21.31],    # Hawaii
            [-155.0, 25.0],     # north of Hawaii
            [-145.0, 30.0],     # eastern Pacific
            [-130.0, 33.0],     # off California
            [-118.19, 33.77],   # Los Angeles
        ],
    },
    {
        "name": "Hawaiki Cable",
        "capacity": "43.8 Tbps",
        "length_km": 15000,
        "rfs": 2018,
        "owners": "Hawaiki Submarine Cable",
        "waypoints": [
            [174.76, -36.85],   # Auckland (Mangawhai), NZ
            [170.0, -35.0],     # off NZ
            [155.0, -33.5],     # Tasman Sea
            [151.21, -33.87],   # Sydney, Australia (branch)
            [170.0, -30.0],     # back across Tasman
            [180.0, -25.0],     # Pacific
            [-175.0, -15.0],    # American Samoa area
            [-165.0, -5.0],     # central Pacific
            [-158.0, 21.31],    # Hawaii
            [-150.0, 30.0],     # north of Hawaii
            [-140.0, 37.0],     # eastern Pacific
            [-130.0, 42.0],     # off Oregon
            [-124.12, 43.96],   # Oregon coast
        ],
    },
    {
        "name": "PLCN (Pacific Light Cable Network)",
        "capacity": "144 Tbps",
        "length_km": 12800,
        "rfs": 2022,
        "owners": "Google/Meta (Hong Kong section modified)",
        "waypoints": [
            [114.17, 22.25],    # Hong Kong area
            [118.0, 22.0],      # South China Sea
            [120.5, 17.0],      # off Luzon
            [130.0, 15.0],      # Philippine Sea
            [144.8, 13.5],      # Guam area
            [155.0, 18.0],      # mid-Pacific
            [170.0, 22.0],      # central Pacific
            [-180.0, 26.0],     # Date Line
            [-165.0, 28.0],     # eastern Pacific
            [-150.0, 30.0],     # mid-Pacific
            [-135.0, 32.0],     # off California approach
            [-118.19, 33.77],   # Los Angeles
        ],
    },
    {
        "name": "AAG (Asia-America Gateway)",
        "capacity": "2.88 Tbps",
        "length_km": 20000,
        "rfs": 2009,
        "owners": "AT&T/others",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [104.0, 3.0],       # South China Sea south
            [108.0, 10.0],      # off Vietnam (landing)
            [114.17, 22.25],    # Hong Kong (landing)
            [120.5, 14.5],      # Philippines (landing)
            [125.0, 13.0],      # off Philippine east coast
            [135.0, 15.0],      # Philippine Sea
            [144.8, 13.5],      # Guam area
            [155.0, 18.0],      # mid-Pacific
            [-180.0, 21.0],     # Date Line
            [-170.0, 21.0],     # central Pacific
            [-158.0, 21.31],    # Hawaii
            [-150.0, 25.0],     # north of Hawaii
            [-140.0, 30.0],     # eastern Pacific
            [-130.0, 33.0],     # off California
            [-118.19, 33.77],   # Los Angeles
        ],
    },
    {
        "name": "Japan-US Cable (JUS)",
        "capacity": "640 Gbps",
        "length_km": 21000,
        "rfs": 2001,
        "owners": "NTT/IDC/Sprint",
        "waypoints": [
            [139.69, 35.68],    # Japan
            [142.0, 35.0],      # off Japan
            [155.0, 35.0],      # Pacific
            [170.0, 35.0],      # mid-Pacific
            [-180.0, 35.0],     # Date Line
            [-165.0, 35.0],     # eastern Pacific
            [-150.0, 34.0],     # mid-Pacific
            [-135.0, 33.5],     # approaching US
            [-120.0, 33.5],     # off California
            [-118.19, 33.77],   # Los Angeles
        ],
    },
    {
        "name": "Tui-Samoa Cable",
        "capacity": "4 Tbps",
        "length_km": 1500,
        "rfs": 2018,
        "owners": "Samoa Government/ADB",
        "waypoints": [
            [178.0, -18.14],    # Suva, Fiji
            [180.0, -17.5],     # east of Fiji
            [-179.0, -17.0],    # mid-ocean
            [-175.0, -15.0],    # Tonga area
            [-172.0, -13.8],    # Apia, Samoa
        ],
    },
    {
        "name": "Tasman Global Access (TGA)",
        "capacity": "20 Tbps",
        "length_km": 2300,
        "rfs": 2017,
        "owners": "Spark NZ/Telstra",
        "waypoints": [
            [151.21, -33.87],   # Sydney, Australia
            [155.0, -34.0],     # off Sydney
            [162.0, -36.0],     # Tasman Sea
            [170.0, -38.0],     # approaching NZ
            [174.76, -36.85],   # Auckland, NZ
        ],
    },
    {
        "name": "HANTRU-1 (Hawaii-American Samoa)",
        "capacity": "2 Tbps",
        "length_km": 4800,
        "rfs": 2010,
        "owners": "ASH Cable",
        "waypoints": [
            [-158.0, 21.31],    # Hawaii
            [-160.0, 15.0],     # south of Hawaii
            [-165.0, 5.0],      # central Pacific
            [-170.0, -5.0],     # equatorial Pacific
            [-170.7, -14.28],   # Pago Pago, American Samoa
        ],
    },
    {
        "name": "JGA (Japan-Guam-Australia)",
        "capacity": "36 Tbps",
        "length_km": 9500,
        "rfs": 2020,
        "owners": "RTI/NEC",
        "waypoints": [
            [139.69, 35.68],    # Japan
            [140.0, 30.0],      # south of Japan
            [141.5, 24.0],      # off Bonin Islands
            [143.0, 18.0],      # approaching Guam
            [144.8, 13.5],      # Guam
            [145.0, 8.0],       # Micronesia
            [147.0, 0.0],       # equatorial Pacific
            [150.0, -5.0],      # off Papua New Guinea
            [152.0, -15.0],     # Coral Sea
            [153.0, -27.0],     # off Queensland
            [151.21, -33.87],   # Sydney, Australia
        ],
    },
    {
        "name": "SEA-US (SE Asia-US)",
        "capacity": "20 Tbps",
        "length_km": 15000,
        "rfs": 2017,
        "owners": "RTI Connectivity/others",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [105.0, 5.0],       # South China Sea
            [110.0, 10.0],      # off Vietnam
            [115.0, 14.0],      # Philippine Sea approach
            [121.0, 14.5],      # Manila, Philippines
            [130.0, 13.0],      # off Philippine east coast
            [144.8, 13.5],      # Guam
            [155.0, 18.0],      # mid-Pacific
            [-180.0, 21.0],     # Date Line
            [-170.0, 21.0],     # central Pacific
            [-158.0, 21.31],    # Hawaii
            [-150.0, 25.0],     # north of Hawaii
            [-140.0, 30.0],     # eastern Pacific
            [-118.19, 33.77],   # Los Angeles
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # SE Asia <-> Europe (SEA-ME-WE corridor)
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "SEA-ME-WE 3",
        "capacity": "480 Gbps",
        "length_km": 39000,
        "rfs": 1999,
        "owners": "France Telecom/SingTel/others",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [100.5, 5.0],       # Strait of Malacca
            [96.0, 7.0],        # Andaman Sea
            [82.0, 6.5],        # off Sri Lanka south
            [80.22, 5.95],      # around Sri Lanka tip
            [76.0, 8.5],        # off Kerala, India
            [73.0, 10.0],       # Lakshadweep Sea
            [66.0, 15.0],       # Arabian Sea
            [57.0, 21.5],       # off Oman
            [52.0, 23.5],       # Persian Gulf approach
            [48.5, 26.0],       # off Bahrain
            [43.3, 12.5],       # Bab el-Mandeb
            [42.0, 14.0],       # Red Sea south
            [39.0, 20.0],       # Red Sea central
            [36.0, 25.0],       # Red Sea north
            [33.0, 28.0],       # Gulf of Suez
            [32.5, 30.0],       # Suez Canal area
            [32.0, 31.5],       # Port Said, Egypt
            [30.0, 33.0],       # eastern Mediterranean
            [25.0, 35.5],       # Crete area
            [18.0, 37.0],       # central Mediterranean
            [10.0, 38.0],       # Tunisia area
            [5.0, 39.0],        # western Mediterranean
            [1.0, 42.0],        # Gulf of Lion
            [1.59, 50.85],      # Calais/Penmarch, France
        ],
    },
    {
        "name": "SEA-ME-WE 4",
        "capacity": "1.28 Tbps",
        "length_km": 20000,
        "rfs": 2005,
        "owners": "SingTel/Telia/others",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [100.5, 5.0],       # Strait of Malacca
            [92.0, 8.0],        # Andaman Sea
            [81.0, 6.0],        # off Sri Lanka
            [80.22, 5.95],      # Sri Lanka south tip
            [77.0, 8.0],        # off Cochin, India
            [72.88, 18.93],     # Mumbai, India
            [66.0, 21.0],       # off Pakistan
            [57.0, 23.5],       # off Oman
            [55.0, 25.0],       # off UAE
            [51.5, 25.9],       # Ras Laffan, Qatar area
            [43.3, 12.5],       # Bab el-Mandeb
            [39.0, 20.0],       # Red Sea
            [35.0, 25.0],       # Red Sea north
            [32.5, 30.0],       # Suez
            [32.0, 31.5],       # Port Said
            [28.0, 34.0],       # eastern Mediterranean
            [18.0, 36.0],       # central Med
            [10.0, 38.0],       # western Med
            [5.37, 43.30],      # Marseille, France
        ],
    },
    {
        "name": "SEA-ME-WE 5",
        "capacity": "24 Tbps",
        "length_km": 20000,
        "rfs": 2017,
        "owners": "SingTel/France Telecom/others",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [100.5, 5.0],       # Strait of Malacca
            [92.0, 8.0],        # Andaman Sea
            [85.0, 7.0],        # Bay of Bengal
            [80.22, 5.95],      # Sri Lanka
            [76.0, 10.0],       # off Kerala
            [72.88, 18.93],     # Mumbai landing
            [66.0, 21.0],       # Arabian Sea
            [58.0, 23.5],       # Oman
            [51.5, 26.0],       # Qatar
            [43.3, 12.5],       # Bab el-Mandeb
            [39.0, 20.0],       # Red Sea
            [35.5, 26.0],       # Red Sea north
            [32.5, 30.0],       # Suez
            [32.0, 31.5],       # Port Said
            [25.0, 35.0],       # Crete
            [16.0, 38.0],       # southern Italy
            [10.0, 40.0],       # central Med
            [5.37, 43.30],      # Toulon/Marseille, France
        ],
    },
    {
        "name": "SEA-ME-WE 6",
        "capacity": "100+ Tbps",
        "length_km": 19200,
        "rfs": 2025,
        "owners": "China Mobile/SingTel/others",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [100.5, 5.0],       # Strait of Malacca
            [92.0, 8.0],        # Andaman Sea
            [85.0, 7.0],        # Bay of Bengal
            [80.22, 5.95],      # Sri Lanka
            [73.0, 12.0],       # off western India
            [66.0, 18.0],       # Arabian Sea
            [56.0, 22.0],       # Oman approach
            [48.0, 25.0],       # Gulf
            [43.3, 12.5],       # Bab el-Mandeb
            [39.0, 20.0],       # Red Sea
            [35.5, 26.0],       # Red Sea north
            [32.5, 30.0],       # Suez
            [32.0, 31.5],       # Port Said
            [25.0, 35.5],       # Crete
            [18.0, 38.0],       # Ionian Sea
            [13.5, 40.5],       # off Naples
            [12.5, 45.44],      # Venice, Italy
        ],
    },
    {
        "name": "PEACE Cable",
        "capacity": "96 Tbps",
        "length_km": 15000,
        "rfs": 2022,
        "owners": "PEACE Cable International/Hengtong",
        "waypoints": [
            [66.99, 24.87],     # Karachi, Pakistan
            [60.0, 23.5],       # off Oman
            [54.0, 22.0],       # off UAE
            [48.0, 20.0],       # off Yemen
            [43.3, 12.5],       # Bab el-Mandeb
            [42.0, 14.5],       # Red Sea south
            [39.0, 20.0],       # Red Sea central
            [36.0, 26.0],       # Red Sea north
            [32.5, 30.0],       # Suez
            [32.0, 31.5],       # Port Said
            [28.0, 34.0],       # eastern Med
            [20.0, 37.0],       # central Med
            [12.0, 40.0],       # western Med
            [5.37, 43.30],      # Marseille, France
        ],
    },
    {
        "name": "FLAG Europe-Asia (FEA)",
        "capacity": "10 Tbps",
        "length_km": 28000,
        "rfs": 1997,
        "owners": "Reliance Globalcom",
        "waypoints": [
            [-4.55, 50.83],     # Bude, UK
            [-5.0, 48.0],       # Bay of Biscay
            [-2.0, 43.5],       # northern Spain
            [-5.35, 36.0],      # Strait of Gibraltar
            [0.0, 37.0],        # western Med
            [10.0, 37.5],       # central Med
            [25.0, 34.5],       # Crete
            [32.0, 31.5],       # Port Said, Egypt
            [32.5, 30.0],       # Suez
            [36.0, 25.0],       # Red Sea
            [39.0, 20.0],       # Red Sea
            [43.3, 12.5],       # Bab el-Mandeb
            [48.0, 18.0],       # Gulf of Aden
            [56.0, 22.0],       # off Oman
            [66.0, 23.0],       # off Pakistan
            [72.88, 18.93],     # Mumbai, India
            [80.22, 5.95],      # Sri Lanka
            [92.0, 8.0],        # Andaman Sea
            [100.0, 5.0],       # Strait of Malacca
            [103.82, 1.35],     # Singapore
            [105.0, 7.0],       # South China Sea
            [114.17, 22.25],    # Hong Kong
            [121.5, 25.0],      # Taiwan
            [130.0, 33.0],      # East China Sea
            [139.69, 35.68],    # Japan
        ],
    },
    {
        "name": "IMEWE (India-ME-Western Europe)",
        "capacity": "3.84 Tbps",
        "length_km": 12091,
        "rfs": 2010,
        "owners": "Tata/BSNL/Ogero/Etisalat/others",
        "waypoints": [
            [72.88, 18.93],     # Mumbai, India
            [66.0, 21.0],       # Arabian Sea
            [56.32, 25.23],     # Fujairah, UAE
            [48.0, 26.0],       # Persian Gulf
            [43.3, 12.5],       # Bab el-Mandeb
            [39.0, 20.0],       # Red Sea
            [36.0, 25.0],       # Red Sea north
            [35.5, 33.9],       # Beirut, Lebanon
            [32.0, 31.5],       # Port Said area
            [28.0, 34.0],       # eastern Med
            [18.0, 36.0],       # central Med
            [10.0, 38.0],       # western Med
            [5.37, 43.30],      # Marseille, France
        ],
    },
    {
        "name": "AAE-1 (Asia-Africa-Europe)",
        "capacity": "25 Tbps",
        "length_km": 25000,
        "rfs": 2017,
        "owners": "PCCW/others",
        "waypoints": [
            [114.17, 22.25],    # Hong Kong
            [110.0, 16.0],      # South China Sea
            [106.0, 10.0],      # off Ho Chi Minh City
            [103.82, 1.35],     # Singapore
            [96.0, 7.0],        # Andaman Sea
            [80.22, 5.95],      # Sri Lanka
            [72.88, 18.93],     # Mumbai
            [66.0, 21.0],       # Arabian Sea
            [56.32, 25.23],     # UAE
            [43.3, 12.5],       # Bab el-Mandeb / Djibouti
            [39.0, 20.0],       # Red Sea
            [36.0, 26.0],       # Red Sea north
            [32.5, 30.0],       # Suez
            [32.0, 31.5],       # Port Said
            [25.0, 35.5],       # Crete
            [16.0, 38.0],       # Italy approach
            [5.37, 43.30],      # Marseille, France
        ],
    },
    {
        "name": "Europe India Gateway (EIG)",
        "capacity": "3.84 Tbps",
        "length_km": 15000,
        "rfs": 2011,
        "owners": "Tata/Airtel/BT/others",
        "waypoints": [
            [72.88, 18.93],     # Mumbai, India
            [66.0, 21.0],       # Arabian Sea
            [56.32, 25.23],     # Fujairah, UAE
            [43.3, 12.5],       # Djibouti/Bab el-Mandeb
            [39.0, 20.0],       # Red Sea
            [36.0, 25.0],       # Red Sea north
            [32.5, 30.0],       # Suez
            [32.0, 31.5],       # Port Said
            [28.0, 34.0],       # eastern Med
            [15.5, 38.2],       # Sicily
            [9.5, 40.0],        # Sardinia
            [-5.35, 36.0],      # Strait of Gibraltar
            [-9.14, 38.72],     # Lisbon area
            [-5.0, 50.2],       # Cornwall approach
            [-4.55, 50.83],     # Bude, UK
        ],
    },
    {
        "name": "TGN-Eurasia",
        "capacity": "2 Tbps",
        "length_km": 15000,
        "rfs": 2001,
        "owners": "Telia Carrier",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [100.0, 5.0],       # Strait of Malacca
            [92.0, 8.0],        # Andaman Sea
            [80.22, 5.95],      # Sri Lanka
            [72.88, 18.93],     # Mumbai
            [66.0, 21.0],       # Arabian Sea
            [55.0, 23.0],       # off UAE
            [43.3, 12.5],       # Bab el-Mandeb
            [39.0, 20.0],       # Red Sea
            [32.5, 30.0],       # Suez
            [32.0, 31.5],       # Mediterranean entry
            [25.0, 35.5],       # Crete
            [10.0, 38.0],       # central Med
            [5.37, 43.30],      # Marseille, France
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Africa circumnavigation / coastal
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "2Africa",
        "capacity": "180 Tbps",
        "length_km": 45000,
        "rfs": 2024,
        "owners": "Meta/MTN/Vodafone/others",
        "waypoints": [
            [-4.55, 50.83],     # Bude, UK
            [-9.14, 38.72],     # Lisbon, Portugal
            [-13.0, 33.0],      # off Morocco
            [-16.0, 28.0],      # Canary Islands area
            [-17.47, 14.69],    # Dakar, Senegal
            [-15.0, 10.0],      # off Guinea
            [-10.0, 6.5],       # off Sierra Leone / Liberia
            [-5.0, 5.0],        # off Ivory Coast
            [-4.02, 5.32],      # Abidjan, Ivory Coast
            [-1.6, 5.0],        # off Ghana
            [1.0, 6.0],         # off Togo / Benin
            [3.39, 6.45],       # Lagos, Nigeria
            [8.5, 4.0],         # off Cameroon
            [9.4, 0.4],         # Libreville, Gabon
            [12.0, -4.3],       # Pointe-Noire, Congo
            [13.23, -8.84],     # Luanda, Angola
            [12.0, -15.0],      # off Namibia north
            [14.0, -22.6],      # Walvis Bay, Namibia
            [15.0, -28.0],      # off Namibia south
            [18.42, -33.92],    # Cape Town, South Africa
            [28.0, -32.0],      # off Eastern Cape
            [31.0, -29.9],      # Durban, South Africa
            [32.57, -25.97],    # Maputo, Mozambique
            [35.0, -23.0],      # off Mozambique
            [39.28, -6.79],     # Dar es Salaam, Tanzania
            [39.67, -4.05],     # Mombasa, Kenya
            [45.0, 2.0],        # off Mogadishu, Somalia
            [43.15, 11.59],     # Djibouti
            [43.3, 12.5],       # Bab el-Mandeb
            [39.0, 20.0],       # Red Sea central
            [38.0, 22.0],       # Jeddah approach
            [39.16, 21.52],     # Jeddah, Saudi Arabia
            [35.0, 29.52],      # Aqaba, Jordan
            [32.5, 30.0],       # Suez Canal
            [32.0, 31.5],       # Port Said
            [28.0, 34.0],       # eastern Med
            [24.0, 35.0],       # Crete
            [18.0, 38.0],       # Ionian
            [12.0, 40.0],       # off Naples
            [8.93, 44.41],      # Genoa, Italy
            [5.37, 43.30],      # Marseille, France
            [2.17, 41.38],      # Barcelona, Spain
            [-9.14, 38.72],     # back to Lisbon
            [-4.55, 50.83],     # Bude, UK
        ],
    },
    {
        "name": "WACS (West Africa Cable System)",
        "capacity": "14.5 Tbps",
        "length_km": 14530,
        "rfs": 2012,
        "owners": "Vodacom/MTN/others",
        "waypoints": [
            [-4.55, 50.83],     # Bude, UK
            [-9.14, 38.72],     # Lisbon, Portugal
            [-13.0, 33.0],      # off Morocco
            [-16.0, 28.0],      # off Western Sahara
            [-17.0, 20.0],      # off Mauritania
            [-17.47, 14.69],    # Dakar, Senegal
            [-15.0, 10.0],      # off Guinea
            [-10.0, 6.5],       # off Liberia
            [-5.0, 5.0],        # off Ivory Coast
            [-1.6, 5.0],        # off Ghana
            [1.0, 6.0],         # off Togo
            [3.39, 6.45],       # Lagos, Nigeria
            [8.5, 4.0],         # off Cameroon
            [9.4, 0.4],         # Gabon
            [12.0, -4.3],       # Congo
            [13.23, -8.84],     # Luanda, Angola
            [12.0, -15.0],      # off Namibia
            [15.0, -22.6],      # Namibia coast
            [18.42, -33.92],    # Cape Town, South Africa
        ],
    },
    {
        "name": "SAT-3/WASC",
        "capacity": "340 Gbps",
        "length_km": 14350,
        "rfs": 2002,
        "owners": "Telkom SA/others",
        "waypoints": [
            [18.42, -33.92],    # Cape Town, South Africa
            [15.0, -22.6],      # off Namibia
            [13.23, -8.84],     # Luanda, Angola
            [12.0, -4.3],       # Congo
            [9.4, 0.4],         # Gabon
            [8.5, 4.0],         # Cameroon
            [3.39, 6.45],       # Lagos, Nigeria
            [-1.6, 5.0],        # Ghana
            [-5.0, 5.0],        # Ivory Coast
            [-17.47, 14.69],    # Dakar, Senegal
            [-17.0, 20.0],      # off Mauritania
            [-13.0, 33.0],      # off Morocco
            [-9.14, 38.72],     # Lisbon, Portugal
            [-8.4, 43.4],       # Vigo, Spain
        ],
    },
    {
        "name": "MainOne",
        "capacity": "10 Tbps",
        "length_km": 7000,
        "rfs": 2010,
        "owners": "MainOne/Equinix",
        "waypoints": [
            [-9.14, 38.72],     # Lisbon, Portugal
            [-13.0, 33.0],      # off Morocco
            [-16.0, 28.0],      # off Western Sahara
            [-17.0, 20.0],      # off Mauritania
            [-17.47, 14.69],    # Dakar, Senegal (branch)
            [-5.0, 5.0],        # off Ivory Coast
            [-1.6, 5.0],        # off Ghana / Accra
            [3.39, 6.45],       # Lagos, Nigeria
        ],
    },
    {
        "name": "ACE (Africa Coast to Europe)",
        "capacity": "12.8 Tbps",
        "length_km": 17000,
        "rfs": 2012,
        "owners": "Orange/others",
        "waypoints": [
            [-1.15, 46.15],     # La Rochelle, France
            [-5.0, 43.0],       # Bay of Biscay
            [-9.14, 38.72],     # Lisbon area
            [-13.0, 33.0],      # off Morocco
            [-16.0, 28.0],      # Canary Islands
            [-17.0, 20.0],      # off Mauritania
            [-17.47, 14.69],    # Dakar, Senegal
            [-16.0, 13.0],      # Gambia area
            [-15.0, 10.5],      # off Guinea
            [-13.0, 8.5],       # Sierra Leone
            [-10.8, 6.3],       # Monrovia, Liberia
            [-5.0, 5.0],        # Ivory Coast
            [-1.6, 5.0],        # Ghana
            [1.0, 6.0],         # Togo/Benin
            [3.39, 6.45],       # Lagos, Nigeria
            [8.5, 4.0],         # Cameroon
            [9.4, 0.4],         # Gabon
            [11.5, -4.0],       # Congo
            [13.23, -8.84],     # Angola
            [12.0, -15.0],      # Namibia north
            [18.42, -33.92],    # Cape Town, South Africa
        ],
    },
    {
        "name": "Equiano (Google)",
        "capacity": "144 Tbps",
        "length_km": 15000,
        "rfs": 2023,
        "owners": "Google",
        "waypoints": [
            [-9.14, 38.72],     # Lisbon, Portugal
            [-13.0, 33.0],      # off Morocco
            [-16.0, 28.0],      # off Western Sahara
            [-17.47, 14.69],    # Dakar area (branch)
            [-5.0, 5.0],        # off Ivory Coast
            [1.0, 6.0],         # off Togo
            [3.39, 6.45],       # Lagos, Nigeria
            [8.5, 4.0],         # Cameroon
            [9.4, 0.4],         # Gabon
            [12.0, -4.3],       # Congo
            [13.23, -8.84],     # Angola
            [12.0, -15.0],      # off Namibia
            [14.0, -22.6],      # Namibia coast
            [18.42, -33.92],    # Cape Town, South Africa
        ],
    },
    {
        "name": "EASSy (Eastern Africa Submarine Cable System)",
        "capacity": "10 Tbps",
        "length_km": 10000,
        "rfs": 2010,
        "owners": "WIOCC/others",
        "waypoints": [
            [18.42, -33.92],    # Cape Town (via Mtunzini)
            [28.0, -32.0],      # off Eastern Cape
            [31.0, -29.9],      # Durban
            [32.57, -25.97],    # Maputo, Mozambique
            [35.0, -23.0],      # off Mozambique
            [40.0, -15.0],      # off northern Mozambique
            [39.28, -6.79],     # Dar es Salaam, Tanzania
            [39.67, -4.05],     # Mombasa, Kenya
            [42.0, 0.0],        # off Somalia south
            [45.0, 2.0],        # Mogadishu area
            [43.15, 11.59],     # Djibouti
        ],
    },
    {
        "name": "SEACOM",
        "capacity": "1.5 Tbps",
        "length_km": 17000,
        "rfs": 2009,
        "owners": "SEACOM Ltd",
        "waypoints": [
            [18.42, -33.92],    # Cape Town (Melkbosstrand)
            [28.0, -32.0],      # off Eastern Cape
            [32.57, -25.97],    # Maputo, Mozambique
            [35.0, -20.0],      # Mozambique Channel
            [39.28, -6.79],     # Dar es Salaam, Tanzania
            [39.67, -4.05],     # Mombasa, Kenya
            [43.15, 11.59],     # Djibouti
            [43.3, 12.5],       # Bab el-Mandeb
            [42.0, 14.5],       # Red Sea
            [39.0, 20.0],       # Red Sea central
            [35.5, 28.0],       # Red Sea north
            [32.5, 30.0],       # Suez
            [32.0, 31.5],       # Port Said
            [25.0, 35.0],       # Crete
            [18.0, 38.0],       # Ionian Sea
            [5.37, 43.30],      # Marseille, France
            [72.88, 18.93],     # Mumbai, India (branch from Djibouti)
        ],
    },
    {
        "name": "TEAMS (The East African Marine System)",
        "capacity": "1.2 Tbps",
        "length_km": 5000,
        "rfs": 2009,
        "owners": "Telkom Kenya/Etisalat",
        "waypoints": [
            [39.67, -4.05],     # Mombasa, Kenya
            [42.0, 0.0],        # off Somalia
            [46.0, 5.0],        # off Horn of Africa
            [50.0, 10.0],       # Arabian Sea
            [55.0, 18.0],       # off Oman
            [56.32, 25.23],     # Fujairah, UAE
        ],
    },
    {
        "name": "DARE (Djibouti Africa Regional Express)",
        "capacity": "36 Tbps",
        "length_km": 5000,
        "rfs": 2022,
        "owners": "Djibouti Telecom/others",
        "waypoints": [
            [43.15, 11.59],     # Djibouti
            [45.0, 8.0],        # off Somalia
            [48.0, 5.0],        # off Somalia south
            [50.0, 2.0],        # Indian Ocean
            [39.67, -4.05],     # Mombasa, Kenya (branch)
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Intra-Asia
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "APG (Asia Pacific Gateway)",
        "capacity": "54.9 Tbps",
        "length_km": 10400,
        "rfs": 2016,
        "owners": "NTT/VNPT/CAT Telecom/others",
        "waypoints": [
            [139.69, 35.68],    # Japan (Chiba)
            [130.0, 33.0],      # off Kyushu
            [120.5, 25.0],      # off Taiwan
            [114.17, 22.25],    # Hong Kong
            [108.0, 16.0],      # Da Nang, Vietnam
            [106.0, 10.0],      # off Ho Chi Minh City
            [104.5, 8.0],       # off southern Vietnam
            [103.82, 1.35],     # Singapore
        ],
    },
    {
        "name": "SJC (Southeast Asia-Japan Cable)",
        "capacity": "28 Tbps",
        "length_km": 9700,
        "rfs": 2013,
        "owners": "Google/SingTel/KDDI/others",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [105.0, 5.0],       # South China Sea south
            [110.0, 10.0],      # off Vietnam
            [114.17, 22.25],    # Hong Kong
            [120.0, 25.0],      # off Taiwan
            [128.0, 30.0],      # off Ryukyu Islands
            [139.69, 35.68],    # Japan (Chiba)
        ],
    },
    {
        "name": "SJC2 (Southeast Asia-Japan Cable 2)",
        "capacity": "144 Tbps",
        "length_km": 10500,
        "rfs": 2023,
        "owners": "PLDT/Meta/others",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [104.0, 5.0],       # South China Sea
            [108.0, 10.0],      # off Vietnam
            [114.17, 22.25],    # Hong Kong (branch)
            [121.0, 14.5],      # Manila, Philippines (branch)
            [121.5, 25.0],      # Taiwan (branch)
            [127.0, 33.0],      # East China Sea
            [130.0, 34.0],      # off Kyushu
            [139.69, 35.68],    # Japan
        ],
    },
    {
        "name": "APCN-2 (Asia Pacific Cable Network-2)",
        "capacity": "2.56 Tbps",
        "length_km": 19000,
        "rfs": 2002,
        "owners": "NTT/Telstra/KT/others",
        "waypoints": [
            [139.69, 35.68],    # Japan
            [129.87, 35.18],    # Busan, South Korea
            [121.5, 25.0],      # Taiwan
            [121.0, 14.5],      # Manila, Philippines
            [114.17, 22.25],    # Hong Kong
            [105.0, 5.0],       # South China Sea
            [103.82, 1.35],     # Singapore
        ],
    },
    {
        "name": "C2C (City-to-City Cable)",
        "capacity": "2.2 Tbps",
        "length_km": 36500,
        "rfs": 2002,
        "owners": "Telia Carrier",
        "waypoints": [
            [139.69, 35.68],    # Japan
            [130.0, 33.0],      # East China Sea
            [121.47, 31.23],    # Shanghai, China
            [114.17, 22.25],    # Hong Kong
            [103.82, 1.35],     # Singapore
        ],
    },
    {
        "name": "EAC-C2C (East Asia Crossing / City-to-City)",
        "capacity": "17.9 Tbps",
        "length_km": 36500,
        "rfs": 2002,
        "owners": "Telia/PCCW",
        "waypoints": [
            [139.69, 35.68],    # Japan
            [129.87, 35.18],    # South Korea
            [122.0, 37.0],      # Yellow Sea
            [121.47, 31.23],    # Shanghai, China
            [114.17, 22.25],    # Hong Kong
        ],
    },
    {
        "name": "ASE (Asia Submarine Express)",
        "capacity": "4.8 Tbps",
        "length_km": 7000,
        "rfs": 2012,
        "owners": "Telkom Indonesia/Telia",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [105.0, 7.0],       # South China Sea
            [110.0, 12.0],      # off Vietnam/Philippines
            [117.0, 14.5],      # off Luzon
            [121.0, 14.5],      # Manila, Philippines
            [125.0, 20.0],      # off Taiwan east
            [130.0, 30.0],      # off Ryukyu Islands
            [139.69, 35.68],    # Japan
        ],
    },
    {
        "name": "TGN-IA (Tata Global Network - Intra Asia)",
        "capacity": "5.12 Tbps",
        "length_km": 12000,
        "rfs": 2009,
        "owners": "Tata Communications",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [100.0, 5.0],       # Strait of Malacca
            [95.0, 6.0],        # Andaman Sea
            [88.0, 10.0],       # Bay of Bengal
            [80.22, 5.95],      # Sri Lanka
            [77.0, 8.6],        # off Kerala, India
            [72.88, 18.93],     # Mumbai, India
        ],
    },
    {
        "name": "i2i Cable Network",
        "capacity": "8.4 Tbps",
        "length_km": 3200,
        "rfs": 2004,
        "owners": "Reliance Globalcom",
        "waypoints": [
            [80.27, 13.08],     # Chennai, India
            [82.0, 10.0],       # off southeast India
            [85.0, 7.0],        # Bay of Bengal
            [92.0, 5.0],        # Andaman Sea
            [98.0, 3.0],        # Strait of Malacca
            [103.82, 1.35],     # Singapore
        ],
    },
    {
        "name": "BBG (Bangkok-Batam-Guam)",
        "capacity": "2 Tbps",
        "length_km": 7000,
        "rfs": 2002,
        "owners": "CAT Telecom/NTT",
        "waypoints": [
            [100.52, 13.76],    # Bangkok area / Sattahip
            [103.0, 8.0],       # Gulf of Thailand
            [104.5, 3.0],       # South China Sea
            [104.03, 1.05],     # Batam, Indonesia
            [108.0, 5.0],       # South China Sea
            [120.0, 10.0],      # Philippine Sea approach
            [130.0, 12.0],      # western Pacific
            [144.8, 13.5],      # Guam
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Middle East / Indian Ocean
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Oman Australia Cable (OAC)",
        "capacity": "100 Tbps",
        "length_km": 9800,
        "rfs": 2025,
        "owners": "Oman/SubPartners",
        "waypoints": [
            [58.54, 23.61],     # Muscat, Oman
            [60.0, 22.0],       # off Oman coast
            [65.0, 18.0],       # Arabian Sea
            [70.0, 14.0],       # western Indian Ocean
            [76.0, 8.0],        # off Maldives
            [82.0, 3.0],        # central Indian Ocean
            [90.0, -5.0],       # eastern Indian Ocean
            [100.0, -12.0],     # off Indonesia
            [108.0, -18.0],     # eastern Indian Ocean
            [115.0, -25.0],     # off Western Australia
            [115.86, -31.95],   # Perth, Australia
        ],
    },
    {
        "name": "GBI (Gulf Bridge International)",
        "capacity": "2.5 Tbps",
        "length_km": 2200,
        "rfs": 2012,
        "owners": "Gulf Bridge International",
        "waypoints": [
            [48.0, 29.37],      # Kuwait City area
            [50.0, 27.0],       # off Bahrain
            [51.53, 25.92],     # Ras Laffan, Qatar
            [54.0, 25.0],       # off UAE
            [56.32, 25.23],     # Fujairah, UAE
            [57.0, 23.5],       # Oman coast
            [58.54, 23.61],     # Muscat, Oman
            [60.0, 21.0],       # off Oman
            [66.0, 23.0],       # off Pakistan
            [72.88, 18.93],     # Mumbai, India
        ],
    },
    {
        "name": "FALCON (FLAG Alcatel-Lucent Optical Network)",
        "capacity": "3.84 Tbps",
        "length_km": 11200,
        "rfs": 2006,
        "owners": "Reliance Globalcom/FLAG",
        "waypoints": [
            [32.0, 31.5],       # Port Said / Egypt
            [32.5, 30.0],       # Suez
            [36.0, 26.0],       # Red Sea north
            [39.0, 20.0],       # Red Sea
            [43.3, 12.5],       # Bab el-Mandeb
            [44.0, 13.0],       # Gulf of Aden
            [48.0, 20.0],       # off Yemen
            [52.0, 23.5],       # off Oman
            [56.32, 25.23],     # Fujairah, UAE
            [54.0, 25.0],       # UAE coast
            [51.53, 25.92],     # Qatar
            [50.0, 27.0],       # Bahrain
            [48.0, 29.37],      # Kuwait
            [56.32, 25.23],     # back to UAE
            [62.0, 24.0],       # off Pakistan
            [66.99, 24.87],     # Karachi, Pakistan
            [72.88, 18.93],     # Mumbai, India
        ],
    },
    {
        "name": "IOX Cable System",
        "capacity": "40 Tbps",
        "length_km": 7500,
        "rfs": 2024,
        "owners": "Liquid Intelligent Technologies",
        "waypoints": [
            [72.88, 18.93],     # Mumbai, India
            [68.0, 14.0],       # Arabian Sea
            [62.0, 8.0],        # western Indian Ocean
            [57.0, -5.0],       # east of Madagascar
            [57.5, -20.16],     # Port Louis, Mauritius
            [50.0, -25.0],      # south of Madagascar
            [40.0, -30.0],      # Mozambique Channel south
            [31.0, -29.9],      # Durban, South Africa
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Arctic / Nordic
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "ARCTIC CONNECT (planned)",
        "capacity": "200 Tbps",
        "length_km": 14000,
        "rfs": 2027,
        "owners": "Cinia / Arctic Connect",
        "waypoints": [
            [24.94, 60.17],     # Helsinki, Finland
            [25.0, 63.0],       # Gulf of Bothnia
            [28.0, 68.0],       # northern Finland
            [33.0, 69.0],       # Murmansk area
            [40.0, 70.0],       # Barents Sea
            [55.0, 72.0],       # Kara Sea
            [80.0, 73.0],       # along Northern Sea Route
            [110.0, 74.0],      # Laptev Sea
            [140.0, 72.0],      # East Siberian Sea
            [170.0, 68.0],      # Bering Sea approach
            [175.0, 60.0],      # off Kamchatka
            [160.0, 50.0],      # Sea of Okhotsk
            [145.0, 43.0],      # Hokkaido approach
            [139.69, 35.68],    # Tokyo, Japan
        ],
    },
    {
        "name": "Svalbard Undersea Cable System",
        "capacity": "2.5 Tbps",
        "length_km": 1400,
        "rfs": 2004,
        "owners": "Telenor/NASA",
        "waypoints": [
            [6.0, 58.0],        # Stavanger area, Norway
            [5.0, 62.0],        # Norwegian coast
            [6.0, 66.0],        # north Norway
            [10.0, 70.0],       # off Tromso
            [15.0, 74.0],       # Norwegian Sea
            [15.63, 78.22],     # Longyearbyen, Svalbard
        ],
    },
    {
        "name": "Greenland Connect",
        "capacity": "2.88 Tbps",
        "length_km": 4700,
        "rfs": 2009,
        "owners": "Tele Greenland",
        "waypoints": [
            [-21.90, 64.14],    # Reykjavik area, Iceland
            [-28.0, 64.0],      # Denmark Strait
            [-35.0, 63.5],      # off Greenland east coast
            [-46.02, 60.98],    # Nuuk area, Greenland (south tip)
            [-52.0, 56.0],      # Davis Strait
            [-55.0, 52.0],      # off Labrador
            [-59.0, 47.5],      # off Newfoundland
            [-63.57, 44.65],    # Halifax, Canada
        ],
    },
    {
        "name": "CANTAT-3",
        "capacity": "2.5 Gbps",
        "length_km": 7500,
        "rfs": 1994,
        "owners": "Teleglobe/others",
        "waypoints": [
            [-63.57, 44.65],    # Halifax, Canada
            [-55.0, 48.0],      # Grand Banks
            [-40.0, 54.0],      # mid-Atlantic northerly
            [-30.0, 60.0],      # approaching Iceland
            [-21.90, 64.14],    # Iceland
            [-15.0, 62.0],      # Faroe Islands area
            [-7.0, 58.0],       # north Scotland approach
            [-4.55, 50.83],     # Bude, UK
        ],
    },
    {
        "name": "Polar Express (planned)",
        "capacity": "200 Tbps",
        "length_km": 14000,
        "rfs": 2028,
        "owners": "Polar Express Consortium",
        "waypoints": [
            [139.69, 35.68],    # Tokyo, Japan
            [145.0, 40.0],      # off Hokkaido
            [155.0, 50.0],      # Sea of Okhotsk
            [170.0, 60.0],      # Bering Sea
            [-175.0, 65.0],     # Bering Strait area
            [-165.0, 70.0],     # Chukchi Sea
            [-150.0, 72.0],     # Beaufort Sea
            [-120.0, 73.0],     # Northwest Passage
            [-90.0, 72.0],      # Canadian Arctic
            [-60.0, 70.0],      # Baffin Bay
            [-40.0, 65.0],      # off Greenland
            [-20.0, 62.0],      # approaching Europe
            [-10.0, 55.0],      # north Atlantic
            [-9.74, 51.84],     # Ireland
        ],
    },
    {
        "name": "Far North Fiber (planned)",
        "capacity": "192 Tbps",
        "length_km": 17000,
        "rfs": 2027,
        "owners": "Far North Digital/Cinia",
        "waypoints": [
            [139.69, 35.68],    # Tokyo, Japan
            [145.0, 43.0],      # off Hokkaido
            [155.0, 52.0],      # Sea of Okhotsk
            [170.0, 62.0],      # Bering Sea
            [-175.0, 65.0],     # near Bering Strait
            [-165.0, 70.0],     # Chukchi Sea
            [-145.0, 72.0],     # Beaufort Sea
            [-125.0, 73.0],     # Northwest Passage
            [-95.0, 72.5],      # Canadian Arctic
            [-70.0, 69.0],      # Baffin Bay
            [-55.0, 64.0],      # Davis Strait
            [-35.0, 63.0],      # off Greenland
            [-20.0, 63.5],      # near Iceland
            [-10.0, 57.0],      # north Atlantic
            [-9.74, 51.84],     # Ireland
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Australia / Pacific Islands
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Coral Sea Cable System",
        "capacity": "20 Tbps",
        "length_km": 4700,
        "rfs": 2020,
        "owners": "Vocus/Australian Government",
        "waypoints": [
            [151.21, -33.87],   # Sydney, Australia
            [153.0, -27.0],     # off Queensland
            [153.0, -20.0],     # off Townsville
            [150.0, -10.0],     # Coral Sea
            [147.18, -6.73],    # Port Moresby, Papua New Guinea
            [155.0, -6.0],      # Solomon Sea
            [159.95, -9.43],    # Honiara, Solomon Islands
        ],
    },
    {
        "name": "AJC (Australia-Japan Cable)",
        "capacity": "640 Gbps",
        "length_km": 12700,
        "rfs": 2001,
        "owners": "Telstra/KDDI/others",
        "waypoints": [
            [151.21, -33.87],   # Sydney, Australia
            [155.0, -28.0],     # off Queensland
            [157.0, -18.0],     # Coral Sea
            [155.0, -5.0],      # off Papua New Guinea
            [144.8, 13.5],      # Guam
            [143.0, 20.0],      # Philippine Sea
            [140.0, 30.0],      # approaching Japan
            [139.69, 35.68],    # Japan
        ],
    },
    {
        "name": "Indigo (Singapore-Australia)",
        "capacity": "36 Tbps",
        "length_km": 9000,
        "rfs": 2019,
        "owners": "Singtel/Indosat/SubPartners",
        "waypoints": [
            [103.82, 1.35],     # Singapore
            [104.5, -2.0],      # off Sumatra
            [106.0, -6.0],      # Java Sea
            [112.0, -8.0],      # off Bali
            [115.0, -15.0],     # Indian Ocean
            [115.0, -22.0],     # off Western Australia
            [115.86, -31.95],   # Perth, Australia
        ],
    },
    {
        "name": "Pipe Pacific Cable (PPC-1)",
        "capacity": "40 Gbps",
        "length_km": 9700,
        "rfs": 2009,
        "owners": "Pipe Networks/TPG",
        "waypoints": [
            [151.21, -33.87],   # Sydney, Australia
            [155.0, -25.0],     # off Queensland
            [158.0, -15.0],     # Coral Sea
            [155.0, -5.0],      # off Papua New Guinea
            [150.0, 0.0],       # equatorial Pacific
            [145.0, 8.0],       # Micronesia
            [144.8, 13.5],      # Guam
        ],
    },
]

# ═══════════════════════════════════════════════════════════════════════════════
# OIL & GAS PIPELINES
# Waypoints are [longitude, latitude] tracing the approximate overland/subsea route.
# ═══════════════════════════════════════════════════════════════════════════════

OIL_GAS_PIPELINES = [
    # ────────────────────────────────────────────────────────────────────────
    # Russia / Europe
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Druzhba Pipeline",
        "type": "oil",
        "capacity": "1.2M bpd",
        "detail": "World's longest oil pipeline; supplies EU via Belarus/Ukraine",
        "waypoints": [
            [49.10, 52.23],     # Samara, Russia
            [44.0, 53.0],       # Penza area
            [39.0, 53.5],       # Tula area
            [34.0, 53.0],       # Bryansk, Russia
            [28.0, 53.5],       # Mozyr, Belarus (split point)
            # Northern branch:
            [24.0, 53.5],       # Brest, Belarus
            [21.0, 52.2],       # Warsaw area, Poland
            [16.5, 52.4],       # Poznan, Poland
            [14.44, 50.08],     # Schwedt/Prague area, Central Europe
        ],
    },
    {
        "name": "Druzhba Pipeline (Southern Branch)",
        "type": "oil",
        "capacity": "0.4M bpd",
        "detail": "Southern branch through Ukraine to Czech Republic/Hungary",
        "waypoints": [
            [28.0, 53.5],       # Mozyr, Belarus (split from northern)
            [30.5, 50.5],       # Kiev, Ukraine
            [32.0, 49.0],       # central Ukraine
            [28.0, 48.5],       # western Ukraine
            [22.3, 48.6],       # Uzhhorod, Ukraine
            [18.7, 47.5],       # Bratislava, Slovakia
            [16.77, 48.68],     # Baumgarten, Austria
        ],
    },
    {
        "name": "Nord Stream (destroyed)",
        "type": "gas",
        "capacity": "55 bcm/yr",
        "detail": "Sabotaged Sep 2022; twin pipelines on Baltic Sea floor",
        "waypoints": [
            [30.32, 59.93],     # Vyborg, Russia
            [28.0, 59.7],       # Gulf of Finland
            [24.0, 59.5],       # off Estonia
            [20.0, 58.5],       # off Latvia
            [18.0, 57.5],       # off Gotland, Sweden
            [16.0, 55.5],       # off Bornholm, Denmark
            [13.43, 54.11],     # Greifswald, Germany
        ],
    },
    {
        "name": "Nord Stream 2 (destroyed)",
        "type": "gas",
        "capacity": "55 bcm/yr",
        "detail": "Never entered service; sabotaged Sep 2022 alongside Nord Stream 1",
        "waypoints": [
            [28.38, 59.73],     # Ust-Luga, Russia
            [25.0, 59.5],       # Gulf of Finland
            [21.0, 58.5],       # off Estonia
            [18.5, 57.0],       # off Gotland
            [16.0, 55.5],       # off Bornholm
            [13.43, 54.11],     # Greifswald, Germany
        ],
    },
    {
        "name": "TurkStream",
        "type": "gas",
        "capacity": "31.5 bcm/yr",
        "detail": "Operational; critical EU gas route via Turkey",
        "waypoints": [
            [37.78, 44.62],     # Anapa, Russia
            [37.5, 44.0],       # Black Sea coast
            [35.0, 43.0],       # Black Sea
            [32.0, 42.5],       # central Black Sea
            [30.0, 42.0],       # approaching Turkey
            [28.97, 41.18],     # Kiyikoy, near Istanbul, Turkey
        ],
    },
    {
        "name": "Yamal-Europe",
        "type": "gas",
        "capacity": "33 bcm/yr",
        "detail": "4,107 km; Russia-Belarus-Poland-Germany; flows reversed post-2022",
        "waypoints": [
            [72.0, 67.5],       # Yamal Peninsula, Russia
            [65.0, 65.0],       # Urals approach
            [60.0, 62.0],       # across Urals
            [55.0, 59.0],       # central Russia
            [50.0, 56.5],       # Volga region
            [43.0, 54.0],       # western Russia
            [35.0, 53.5],       # Smolensk area
            [28.0, 53.5],       # Minsk, Belarus area
            [24.0, 53.5],       # Brest, Belarus
            [21.01, 52.23],     # Wloclawek, Poland area
            [16.5, 52.4],       # Poznan
            [14.3, 52.5],       # Mallnow, Germany (border)
        ],
    },
    {
        "name": "Blue Stream",
        "type": "gas",
        "capacity": "16 bcm/yr",
        "detail": "Black Sea subsea pipeline; 396 km offshore section",
        "waypoints": [
            [38.0, 44.6],       # Beregovaya, Russia
            [37.0, 44.0],       # Black Sea coast
            [36.0, 43.0],       # Black Sea
            [35.5, 42.5],       # central Black Sea
            [35.12, 42.03],     # Samsun, Turkey
        ],
    },
    {
        "name": "Soyuz Pipeline",
        "type": "gas",
        "capacity": "26 bcm/yr",
        "detail": "Central Asian gas via Russia to Europe; declining flows post-2022",
        "waypoints": [
            [51.37, 51.16],     # Orenburg, Russia
            [48.0, 51.0],       # Samara area
            [43.0, 52.0],       # central Russia
            [37.0, 52.0],       # western Russia
            [33.0, 50.5],       # Ukraine border
            [30.5, 50.5],       # Kiev area, Ukraine
            [24.0, 49.0],       # western Ukraine
            [20.0, 48.7],       # Slovakia
            [16.77, 48.68],     # Baumgarten, Austria
        ],
    },
    {
        "name": "Southern Gas Corridor (TAP section)",
        "type": "gas",
        "capacity": "10 bcm/yr",
        "detail": "Trans Adriatic Pipeline; Caspian gas to Europe bypassing Russia",
        "waypoints": [
            [26.5, 41.0],       # Turkey-Greece border
            [23.7, 40.6],       # Thessaloniki area
            [22.0, 40.0],       # northern Greece
            [20.0, 40.5],       # Albania border
            [19.8, 40.6],       # Albania
            [19.0, 40.5],       # Adriatic coast
            [18.5, 40.0],       # Adriatic Sea crossing
            [17.40, 40.85],     # Brindisi, Italy
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Caucasus / Turkey / Middle East
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "BTC Pipeline (Baku-Tbilisi-Ceyhan)",
        "type": "oil",
        "capacity": "1.2M bpd",
        "detail": "Major Caspian oil export route avoiding Russia/Iran; 1,768 km",
        "waypoints": [
            [49.87, 40.41],     # Baku, Azerbaijan
            [48.5, 40.5],       # Sangachal terminal
            [46.0, 41.2],       # across Azerbaijan
            [44.79, 41.69],     # Tbilisi, Georgia
            [43.5, 41.5],       # eastern Turkey border
            [42.0, 40.5],       # Erzurum area
            [39.0, 39.0],       # central Turkey
            [36.15, 36.85],     # Ceyhan, Turkey (Mediterranean)
        ],
    },
    {
        "name": "Trans-Anatolian Pipeline (TANAP)",
        "type": "gas",
        "capacity": "16 bcm/yr",
        "detail": "Southern Gas Corridor; feeds TAP to Italy",
        "waypoints": [
            [49.87, 40.41],     # Baku, Azerbaijan
            [46.0, 41.2],       # Azerbaijan-Georgia border
            [44.79, 41.69],     # Tbilisi, Georgia
            [43.0, 41.5],       # Turkey border
            [40.0, 40.0],       # eastern Turkey
            [36.0, 39.5],       # central Anatolia
            [32.85, 39.93],     # Ankara area
            [30.0, 40.0],       # western Turkey
            [27.0, 40.5],       # Turkey-Greece border (Ipsala)
        ],
    },
    {
        "name": "Iran-Turkey Gas Pipeline (Tabriz-Ankara)",
        "type": "gas",
        "capacity": "14 bcm/yr",
        "detail": "Operational since 2001; frequent sabotage on Turkish section",
        "waypoints": [
            [46.29, 38.08],     # Tabriz, Iran
            [44.0, 39.0],       # Turkey border (Bazargan)
            [43.0, 39.5],       # Agri area
            [40.0, 39.5],       # Erzurum
            [36.0, 39.5],       # Sivas
            [32.85, 39.93],     # Ankara, Turkey
        ],
    },
    {
        "name": "East-West Pipeline (Saudi Petroline)",
        "type": "oil",
        "capacity": "5M bpd",
        "detail": "Strategic bypass for Strait of Hormuz",
        "waypoints": [
            [49.98, 25.38],     # Abqaiq, Saudi Arabia
            [47.0, 25.0],       # central Saudi
            [44.0, 24.5],       # Riyadh approach
            [42.0, 24.0],       # central desert
            [40.0, 23.0],       # western Saudi
            [39.16, 21.52],     # Yanbu, Red Sea coast
        ],
    },
    {
        "name": "Dolphin Gas Pipeline",
        "type": "gas",
        "capacity": "19 bcm/yr",
        "detail": "Only cross-border pipeline in the Gulf; also feeds Oman",
        "waypoints": [
            [51.53, 25.92],     # Ras Laffan, Qatar
            [51.8, 25.5],       # off Qatar coast
            [53.0, 25.0],       # offshore
            [54.37, 24.47],     # Taweelah, Abu Dhabi, UAE
            [55.5, 25.0],       # along UAE coast
            [56.0, 25.2],       # Oman border
        ],
    },
    {
        "name": "HBJ Pipeline (India)",
        "type": "gas",
        "capacity": "18 bcm/yr",
        "detail": "India's 2,700 km backbone gas pipeline; GAIL-operated",
        "waypoints": [
            [72.83, 21.17],     # Hazira, Gujarat
            [73.0, 22.5],       # Gujarat interior
            [73.5, 23.5],       # Ahmedabad area
            [75.0, 24.5],       # Rajasthan
            [77.0, 25.5],       # Madhya Pradesh
            [78.5, 26.0],       # Agra area
            [80.0, 26.5],       # Lucknow area
            [80.91, 26.85],     # Jagdishpur, UP
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Russia / Far East
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "ESPO Pipeline (East Siberia-Pacific Ocean)",
        "type": "oil",
        "capacity": "1.6M bpd",
        "detail": "Eastern Siberia-Pacific; feeds China/Japan/Korea; 4,188 km",
        "waypoints": [
            [98.0, 56.0],       # Taishet, Russia
            [104.0, 52.5],      # near Lake Baikal (north side)
            [108.0, 52.0],      # Chita area
            [115.0, 51.5],      # Transbaikal
            [120.0, 50.0],      # along China border
            [127.0, 48.5],      # Khabarovsk area
            [131.0, 47.5],      # Primorsky Krai
            [134.32, 46.94],    # Kozmino, Pacific coast
        ],
    },
    {
        "name": "Power of Siberia",
        "type": "gas",
        "capacity": "38 bcm/yr",
        "detail": "Russia-China gas pipeline; ramping to full capacity; 3,000 km",
        "waypoints": [
            [129.74, 62.04],    # Yakutia (Chayandinskoye field), Russia
            [130.0, 58.0],      # southern Yakutia
            [131.0, 54.0],      # along Amur River approach
            [130.0, 50.0],      # Amur Oblast
            [127.5, 48.5],      # Khabarovsk / Blagoveshchensk
            [126.65, 45.75],    # Heilongjiang crossing, China border
        ],
    },
    {
        "name": "Power of Siberia 2 (planned)",
        "type": "gas",
        "capacity": "50 bcm/yr",
        "detail": "Planned via Mongolia; would redirect EU-bound Yamal gas to China",
        "waypoints": [
            [90.22, 61.52],     # Western Siberia, Russia
            [95.0, 58.0],       # south toward Mongolia
            [100.0, 54.0],      # approaching Mongolia
            [104.0, 50.0],      # Mongolia border
            [107.0, 47.5],      # Ulaanbaatar, Mongolia area
            [112.0, 44.0],      # Inner Mongolia
            [116.41, 40.18],    # Beijing, China
        ],
    },
    {
        "name": "Sakhalin-Khabarovsk-Vladivostok",
        "type": "gas",
        "capacity": "6 bcm/yr",
        "detail": "1,830 km; feeds Russian Far East and potential LNG exports",
        "waypoints": [
            [142.68, 52.03],    # Sakhalin Island, Russia
            [141.0, 48.0],      # Sakhalin south / strait crossing
            [135.0, 48.5],      # mainland coast
            [135.0, 47.0],      # Khabarovsk area
            [133.0, 45.0],      # Primorsky Krai
            [131.87, 43.12],    # Vladivostok, Russia
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # North America
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Keystone Pipeline System",
        "type": "oil",
        "capacity": "590K bpd",
        "detail": "Existing Keystone operational; XL expansion cancelled 2021",
        "waypoints": [
            [-113.81, 52.27],   # Hardisty, Alberta
            [-110.0, 50.0],     # southern Alberta
            [-108.0, 49.0],     # US border (Montana)
            [-105.0, 47.0],     # Montana/Dakotas
            [-100.0, 44.0],     # South Dakota
            [-97.0, 41.0],      # Nebraska (Steele City)
            [-97.0, 38.0],      # Kansas
            [-97.0, 36.0],      # Cushing, Oklahoma
            [-96.0, 34.0],      # southern Oklahoma
            [-95.37, 29.76],    # Houston, Texas
        ],
    },
    {
        "name": "Colonial Pipeline",
        "type": "oil",
        "capacity": "2.5M bpd",
        "detail": "Largest refined products pipeline in US; 2021 ransomware attack",
        "waypoints": [
            [-95.37, 29.76],    # Houston, TX
            [-93.0, 30.2],      # Lake Charles, Louisiana
            [-90.0, 30.5],      # Baton Rouge, Louisiana
            [-88.0, 31.0],      # Mississippi
            [-87.0, 32.5],      # Alabama
            [-84.0, 33.7],      # Atlanta, Georgia
            [-82.0, 34.5],      # South Carolina
            [-80.0, 35.5],      # Charlotte, NC area
            [-79.0, 36.0],      # Greensboro, NC
            [-77.0, 37.5],      # Richmond, Virginia
            [-76.0, 39.0],      # Washington DC area
            [-75.0, 39.5],      # Delaware
            [-74.0, 40.74],     # New York Harbor
        ],
    },
    {
        "name": "Trans-Alaska Pipeline (TAPS)",
        "type": "oil",
        "capacity": "600K bpd",
        "detail": "1,288 km; operational since 1977; declining throughput",
        "waypoints": [
            [-148.33, 70.26],   # Prudhoe Bay, Alaska
            [-148.0, 68.0],     # North Slope
            [-146.0, 66.5],     # Brooks Range
            [-146.0, 65.0],     # Yukon River crossing
            [-147.72, 64.84],   # Fairbanks, Alaska
            [-146.0, 63.5],     # Delta Junction
            [-146.0, 62.0],     # Alaska Range
            [-145.5, 61.0],     # Glennallen area
            [-146.0, 60.0],     # Thompson Pass
            [-146.35, 61.13],   # Valdez, Alaska (marine terminal)
        ],
    },
    {
        "name": "Dakota Access Pipeline (DAPL)",
        "type": "oil",
        "capacity": "750K bpd",
        "detail": "Controversial; operational since 2017; Bakken crude to Patoka",
        "waypoints": [
            [-103.98, 47.92],   # Bakken Formation, ND
            [-101.0, 46.0],     # central North Dakota
            [-99.0, 44.0],      # South Dakota
            [-96.0, 42.5],      # Sioux City area
            [-93.0, 41.5],      # Iowa
            [-90.67, 40.46],    # Patoka, Illinois
        ],
    },
    {
        "name": "Permian Basin - Corpus Christi (Cactus II)",
        "type": "oil",
        "capacity": "670K bpd",
        "detail": "Key Permian export pipeline to Gulf Coast terminals",
        "waypoints": [
            [-102.08, 31.99],   # Permian Basin, TX (Wink area)
            [-100.0, 31.0],     # West Texas
            [-98.5, 29.5],      # San Antonio area
            [-97.40, 27.80],    # Corpus Christi, TX
        ],
    },
    {
        "name": "Enbridge Line 5",
        "type": "oil",
        "capacity": "540K bpd",
        "detail": "Controversial pipeline crossing Straits of Mackinac",
        "waypoints": [
            [-87.09, 46.49],    # Superior, Wisconsin
            [-86.0, 46.0],      # Upper Michigan
            [-85.0, 45.8],      # Mackinac Straits crossing
            [-84.5, 45.0],      # Lower Michigan
            [-83.5, 43.5],      # central Michigan
            [-82.42, 42.98],    # Sarnia, Ontario
        ],
    },
    {
        "name": "TC Energy Mainline (NGTL)",
        "type": "gas",
        "capacity": "28 bcm/yr",
        "detail": "Canada's major west-east natural gas transmission system",
        "waypoints": [
            [-113.81, 52.27],   # Alberta, Canada
            [-110.0, 52.0],     # Saskatchewan
            [-105.0, 50.5],     # central Saskatchewan
            [-97.0, 50.0],      # Manitoba/Winnipeg
            [-90.0, 49.0],      # Thunder Bay, Ontario
            [-80.0, 46.0],      # Sudbury, Ontario
            [-75.70, 45.42],    # Ottawa, Ontario
        ],
    },
    {
        "name": "Trans Mountain Expansion (TMX)",
        "type": "oil",
        "capacity": "890K bpd",
        "detail": "Tripled capacity; completed 2024; crude export to Pacific",
        "waypoints": [
            [-113.49, 53.55],   # Edmonton, Alberta
            [-116.0, 53.0],     # Hinton, Alberta
            [-118.0, 52.5],     # Jasper area
            [-120.0, 52.0],     # BC interior
            [-121.0, 51.0],     # Kamloops area
            [-121.5, 49.5],     # Fraser Valley
            [-122.95, 49.29],   # Burnaby, BC
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Mediterranean / North Africa
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Trans-Mediterranean (Transmed)",
        "type": "gas",
        "capacity": "33 bcm/yr",
        "detail": "Major Algeria-to-Europe gas route via Tunisia; 2,475 km",
        "waypoints": [
            [3.06, 36.75],      # Algiers area, Algeria (Hassi R'Mel)
            [5.0, 36.5],        # northeastern Algeria
            [8.0, 36.5],        # Tunisia border
            [9.5, 37.0],        # Tunis area / Cap Bon
            [11.0, 37.5],       # Mediterranean crossing
            [12.5, 37.5],       # off Sicily
            [15.09, 37.50],     # Mazara del Vallo, Sicily, Italy
        ],
    },
    {
        "name": "GreenStream",
        "type": "gas",
        "capacity": "11 bcm/yr",
        "detail": "Libya-Italy subsea gas pipeline; intermittent due to instability",
        "waypoints": [
            [12.09, 32.90],     # Mellitah, Libya
            [12.5, 34.0],       # Mediterranean
            [13.0, 35.0],       # central Med
            [14.0, 36.5],       # approaching Sicily
            [15.09, 37.50],     # Gela, Sicily, Italy
        ],
    },
    {
        "name": "Medgaz Pipeline",
        "type": "gas",
        "capacity": "8 bcm/yr",
        "detail": "Direct Algeria-Spain subsea pipeline; 210 km offshore",
        "waypoints": [
            [0.08, 35.90],      # Beni Saf, Algeria
            [-0.5, 36.0],       # Mediterranean
            [-1.0, 36.3],       # mid-crossing
            [-1.9, 36.7],       # Almeria approach, Spain
        ],
    },
    {
        "name": "Maghreb-Europe Gas Pipeline (MEG)",
        "type": "gas",
        "capacity": "12 bcm/yr",
        "detail": "Algeria-Morocco-Spain; closed by Algeria 2021 due to diplomatic row",
        "waypoints": [
            [3.06, 36.75],      # Hassi R'Mel, Algeria
            [1.0, 35.5],        # northwestern Algeria
            [-2.0, 34.5],       # Morocco border
            [-5.0, 34.0],       # northern Morocco
            [-5.35, 35.90],     # Strait of Gibraltar crossing
            [-5.34, 36.14],     # Tarifa, Spain
            [-3.7, 37.2],       # Cordoba area, Spain
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Africa
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Trans-Saharan Gas Pipeline (planned)",
        "type": "gas",
        "capacity": "30 bcm/yr",
        "detail": "4,128 km planned pipeline; Nigeria-Niger-Algeria to feed Europe",
        "waypoints": [
            [7.01, 4.76],       # Warri, Nigeria
            [7.5, 9.0],         # central Nigeria
            [8.0, 13.5],        # northern Nigeria / Niger border
            [8.0, 17.0],        # Agadez, Niger
            [5.0, 22.0],        # Sahara
            [3.06, 36.75],      # Algiers/Hassi R'Mel, Algeria
        ],
    },
    {
        "name": "West African Gas Pipeline (WAGP)",
        "type": "gas",
        "capacity": "5 bcm/yr",
        "detail": "Supplies Benin, Togo, Ghana from Nigeria's Escravos field",
        "waypoints": [
            [3.39, 6.45],       # Lagos, Nigeria
            [2.0, 6.3],         # off Benin coast
            [1.0, 6.1],         # off Togo coast
            [-0.19, 5.56],      # Accra / Tema, Ghana
        ],
    },
    {
        "name": "East African Crude Oil Pipeline (EACOP)",
        "type": "oil",
        "capacity": "216K bpd",
        "detail": "1,443 km heated pipeline; world's longest heated crude pipeline",
        "waypoints": [
            [31.46, 1.57],      # Hoima, Uganda (Kabaale)
            [32.0, 0.5],        # central Uganda
            [33.0, -1.0],       # Uganda-Tanzania border
            [34.0, -2.5],       # western Tanzania
            [36.0, -4.0],       # central Tanzania
            [38.0, -5.0],       # eastern Tanzania
            [39.10, -5.07],     # Tanga, Tanzania (coast)
        ],
    },
    {
        "name": "Mozambique-South Africa Pipeline (planned)",
        "type": "gas",
        "capacity": "10 bcm/yr",
        "detail": "Planned pipeline to monetize Rovuma Basin LNG discoveries",
        "waypoints": [
            [40.52, -12.97],    # Cabo Delgado, Mozambique
            [38.0, -15.0],      # central Mozambique
            [35.0, -20.0],      # southern Mozambique
            [32.57, -25.97],    # Maputo
            [30.0, -26.5],      # crossing into South Africa
            [28.04, -26.20],    # Johannesburg, South Africa
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # China / Central Asia / East Asia
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "West-East Gas Pipeline (China)",
        "type": "gas",
        "capacity": "30 bcm/yr",
        "detail": "4,000 km; China's domestic backbone; 3 parallel lines",
        "waypoints": [
            [75.99, 38.93],     # Tarim Basin, Xinjiang
            [80.0, 39.5],       # along Taklamakan north edge
            [87.60, 43.80],     # Urumqi area
            [95.0, 40.0],       # Gansu corridor
            [100.0, 38.0],      # Lanzhou approach
            [103.83, 36.06],    # Lanzhou
            [108.94, 34.26],    # Xi'an
            [113.65, 34.76],    # Zhengzhou
            [117.0, 33.0],      # Anhui
            [121.47, 31.23],    # Shanghai
        ],
    },
    {
        "name": "China-Central Asia Gas Pipeline (Lines A/B/C)",
        "type": "gas",
        "capacity": "55 bcm/yr",
        "detail": "1,833 km across Uzbekistan/Kazakhstan; Turkmen gas to China",
        "waypoints": [
            [61.0, 38.0],       # Turkmenistan (Galkynysh field)
            [64.42, 39.77],     # across Turkmenistan
            [65.0, 40.5],       # Bukhara, Uzbekistan
            [67.0, 41.0],       # Uzbekistan
            [69.0, 41.5],       # Tashkent area
            [72.0, 42.0],       # Kazakhstan border
            [76.0, 43.0],       # southeastern Kazakhstan
            [80.0, 43.5],       # Almaty area
            [82.0, 43.5],       # approaching China
            [87.60, 43.80],     # Khorgos, China (Xinjiang)
        ],
    },
    {
        "name": "China-Myanmar Oil & Gas Pipeline",
        "type": "oil",
        "capacity": "440K bpd oil + 12 bcm/yr gas",
        "detail": "771 km; allows China to bypass Strait of Malacca",
        "waypoints": [
            [93.12, 19.76],     # Kyaukphyu, Myanmar
            [95.0, 20.5],       # central Myanmar
            [97.0, 21.5],       # Mandalay area
            [98.5, 23.0],       # Myanmar-China border
            [100.0, 24.0],      # Yunnan province
            [102.68, 25.04],    # Kunming, China
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # South America
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Bolivia-Brazil Pipeline (GASBOL)",
        "type": "gas",
        "capacity": "30 bcm/yr",
        "detail": "3,150 km; Bolivia's main gas export route; declining production",
        "waypoints": [
            [-63.18, -17.78],   # Santa Cruz, Bolivia
            [-60.0, -18.5],     # Bolivia-Brazil border
            [-57.5, -19.0],     # Corumba, Brazil
            [-55.0, -20.5],     # Mato Grosso do Sul
            [-51.0, -22.0],     # Parana state
            [-48.0, -22.5],     # Sao Paulo state interior
            [-46.63, -23.55],   # Sao Paulo, Brazil
        ],
    },
    {
        "name": "Trans-Andean Pipeline (OTC)",
        "type": "oil",
        "capacity": "113K bpd",
        "detail": "Crosses Andes at 2,500m; crude from Vaca Muerta",
        "waypoints": [
            [-68.13, -38.93],   # Neuquen, Argentina
            [-70.0, -38.0],     # foothills
            [-71.0, -37.5],     # Andes crossing
            [-72.0, -37.0],     # Chilean side
            [-73.08, -36.62],   # Concepcion, Chile
        ],
    },
    {
        "name": "Camisea Pipeline (Peru)",
        "type": "gas",
        "capacity": "12 bcm/yr",
        "detail": "730 km from Amazon jungle across Andes to coast; feeds Peru LNG",
        "waypoints": [
            [-72.68, -11.80],   # Camisea gas fields, Peru
            [-73.5, -12.5],     # Amazon lowlands
            [-75.0, -12.5],     # Andes approach
            [-76.0, -12.5],     # Andes crossing
            [-76.5, -12.2],     # western slopes
            [-77.03, -12.04],   # Lima / Pisco, Peru coast
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Southeast Asia
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Trans-ASEAN Gas Pipeline (Yadana)",
        "type": "gas",
        "capacity": "7 bcm/yr",
        "detail": "346 km; Thailand's largest single gas source; production declining",
        "waypoints": [
            [97.22, 12.34],     # Yadana Field, offshore Myanmar
            [98.0, 12.0],       # offshore approach
            [99.0, 12.5],       # landfall Myanmar/Thailand
            [100.50, 13.76],    # Bangkok, Thailand
        ],
    },
    {
        "name": "Sabah-Sarawak Gas Pipeline (Malaysia)",
        "type": "gas",
        "capacity": "3 bcm/yr",
        "detail": "512 km offshore; feeds Bintulu LNG complex",
        "waypoints": [
            [116.08, 5.95],     # Kota Kinabalu, Sabah
            [114.0, 5.0],       # offshore Sabah
            [112.5, 4.0],       # South China Sea
            [111.84, 2.30],     # Bintulu, Sarawak
        ],
    },

    # ────────────────────────────────────────────────────────────────────────
    # Additional Major Pipelines
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Langeled Pipeline",
        "type": "gas",
        "capacity": "25.5 bcm/yr",
        "detail": "World's longest subsea gas pipeline; Norway to UK; 1,200 km",
        "waypoints": [
            [2.52, 58.89],      # Nyhamna, Norway
            [2.0, 58.5],        # Norwegian shelf
            [1.0, 57.5],        # North Sea
            [0.0, 56.0],        # central North Sea
            [-0.5, 54.5],       # approaching UK
            [-0.24, 53.63],     # Easington, UK
        ],
    },
    {
        "name": "Interconnector (UK-Belgium)",
        "type": "gas",
        "capacity": "20 bcm/yr",
        "detail": "235 km subsea; bidirectional gas flow UK-Belgium",
        "waypoints": [
            [1.43, 51.35],      # Bacton, UK
            [1.5, 51.5],        # off Norfolk
            [2.0, 51.5],        # North Sea
            [3.21, 51.35],      # Zeebrugge, Belgium
        ],
    },
    {
        "name": "Baltic Pipe",
        "type": "gas",
        "capacity": "10 bcm/yr",
        "detail": "Norwegian gas to Poland via Denmark; operational 2022",
        "waypoints": [
            [2.5, 58.0],        # Norwegian North Sea connection
            [5.0, 57.0],        # off Denmark west
            [8.0, 56.0],        # Denmark west coast
            [10.5, 55.5],       # across Denmark
            [12.0, 55.5],       # Great Belt area
            [14.5, 54.5],       # off Poland
            [16.0, 54.0],       # Niechorze, Poland
        ],
    },
    {
        "name": "Turkmenistan-Afghanistan-Pakistan-India (TAPI) (planned)",
        "type": "gas",
        "capacity": "33 bcm/yr",
        "detail": "1,800 km planned; Galkynysh field to Fazilka, India",
        "waypoints": [
            [62.0, 37.5],       # Galkynysh field, Turkmenistan
            [65.0, 36.0],       # Herat, Afghanistan
            [66.0, 34.0],       # central Afghanistan
            [66.0, 31.5],       # Quetta area, Pakistan
            [68.0, 28.0],       # Sindh, Pakistan
            [71.0, 26.0],       # Rajasthan border
            [74.0, 30.5],       # Fazilka, India
        ],
    },
    {
        "name": "Iraq-Turkey (Kirkuk-Ceyhan) Pipeline",
        "type": "oil",
        "capacity": "1.6M bpd",
        "detail": "Main Iraqi crude export route north; 970 km; frequently disrupted",
        "waypoints": [
            [44.39, 35.47],     # Kirkuk, Iraq
            [44.0, 36.5],       # northern Iraq
            [43.0, 37.0],       # Iraqi Kurdistan
            [42.0, 37.5],       # Turkey border
            [40.0, 37.5],       # southeastern Turkey
            [37.0, 37.0],       # south-central Turkey
            [36.15, 36.85],     # Ceyhan, Turkey
        ],
    },
    {
        "name": "TransCanada / TC Energy Mainline Gas",
        "type": "gas",
        "capacity": "30 bcm/yr",
        "detail": "Longest pipeline in North America; Western Canada to Quebec/US border",
        "waypoints": [
            [-114.0, 52.0],     # Alberta gathering
            [-110.0, 52.0],     # eastern Alberta
            [-105.0, 50.5],     # Saskatchewan
            [-97.0, 50.0],      # Manitoba / Winnipeg
            [-90.0, 49.0],      # Ontario
            [-85.0, 48.0],      # northern Ontario
            [-80.0, 46.0],      # Sudbury
            [-75.0, 45.5],      # Ottawa
            [-73.56, 45.50],    # Montreal, Quebec
        ],
    },
    {
        "name": "Rockies Express Pipeline (REX)",
        "type": "gas",
        "capacity": "16 bcm/yr",
        "detail": "1,679 miles; Rocky Mountain gas to eastern US",
        "waypoints": [
            [-110.0, 41.5],     # Opal, Wyoming
            [-107.0, 41.0],     # southern Wyoming
            [-104.0, 40.0],     # Colorado
            [-100.0, 39.5],     # Kansas
            [-97.0, 39.0],      # Missouri
            [-93.0, 39.5],      # central Missouri
            [-89.0, 39.5],      # Illinois
            [-84.5, 39.8],      # Clarington, Ohio
        ],
    },
    {
        "name": "Nord-European Gas Pipeline (NEL)",
        "type": "gas",
        "capacity": "20 bcm/yr",
        "detail": "From Nord Stream landfall to western Germany; onshore link",
        "waypoints": [
            [13.43, 54.11],     # Greifswald, Germany
            [12.0, 53.8],       # Mecklenburg
            [10.5, 53.5],       # Hamburg area
            [9.0, 53.0],        # Lower Saxony
            [8.0, 52.5],        # Rehden, Germany (gas hub)
        ],
    },
    {
        "name": "South Caucasus Pipeline (SCP)",
        "type": "gas",
        "capacity": "20 bcm/yr",
        "detail": "Part of Southern Gas Corridor; Azerbaijan through Georgia to Turkey",
        "waypoints": [
            [49.87, 40.41],     # Sangachal, Azerbaijan
            [48.0, 40.5],       # western Azerbaijan
            [46.0, 41.2],       # Georgia border
            [44.79, 41.69],     # Tbilisi, Georgia
            [43.0, 41.5],       # Turkey border
            [42.7, 41.0],       # Erzurum, Turkey
        ],
    },
]

# ═══════════════════════════════════════════════════════════════════════════════
# OIL FIELDS & RARE EARTH / CRITICAL MINERAL DEPOSITS
# radius_km reflects approximate real extent of the field/deposit.
# ═══════════════════════════════════════════════════════════════════════════════

OIL_RARE_EARTH_FIELDS = [
    # ────────────────────────────────────────────────────────────────────────
    # Major Oil Fields
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Ghawar Field",
        "country": "SA",
        "lat": 25.38,
        "lng": 49.40,
        "type": "oil",
        "reserves": "~75B barrels",
        "radius_km": 130,
        "detail": "World's largest conventional oil field; Saudi Aramco; 3.8M bpd peak",
    },
    {
        "name": "Burgan Field",
        "country": "KW",
        "lat": 28.98,
        "lng": 47.98,
        "type": "oil",
        "reserves": "~70B barrels",
        "radius_km": 40,
        "detail": "World's 2nd-largest oil field; Kuwait Petroleum Corp; 1.7M bpd capacity",
    },
    {
        "name": "Safaniya Field",
        "country": "SA",
        "lat": 27.85,
        "lng": 49.70,
        "type": "oil",
        "reserves": "~37B barrels",
        "radius_km": 50,
        "detail": "World's largest offshore oil field; Arabian heavy crude; Saudi Aramco",
    },
    {
        "name": "Prudhoe Bay",
        "country": "US",
        "lat": 70.26,
        "lng": -148.33,
        "type": "oil",
        "reserves": "~25B barrels (original)",
        "radius_km": 50,
        "detail": "North America's largest oil field; Trans-Alaska Pipeline source; declining production",
    },
    {
        "name": "Cantarell Complex",
        "country": "MX",
        "lat": 20.17,
        "lng": -91.58,
        "type": "oil",
        "reserves": "~18B barrels (original)",
        "radius_km": 35,
        "detail": "Once world's 2nd-largest; dramatic decline from 2.1M bpd (2004) to ~100K bpd",
    },
    {
        "name": "Pre-salt Santos Basin",
        "country": "BR",
        "lat": -25.25,
        "lng": -44.50,
        "type": "oil",
        "reserves": "~15B barrels",
        "radius_km": 100,
        "detail": "Ultra-deepwater; Lula/Buzios fields; Petrobras; 5-7 km below sea level",
    },
    {
        "name": "Kashagan Field",
        "country": "KZ",
        "lat": 46.10,
        "lng": 51.50,
        "type": "oil",
        "reserves": "~13B barrels",
        "radius_km": 30,
        "detail": "World's most expensive oil project ($55B); toxic H2S; Caspian Sea",
    },
    {
        "name": "West Qurna Field",
        "country": "IQ",
        "lat": 30.95,
        "lng": 47.30,
        "type": "oil",
        "reserves": "~43B barrels",
        "radius_km": 35,
        "detail": "World's 4th-largest; phases 1&2; ExxonMobil/Lukoil/CNPC operators",
    },
    {
        "name": "Permian Basin",
        "country": "US",
        "lat": 31.90,
        "lng": -102.30,
        "type": "oil",
        "reserves": "~46B barrels (recoverable)",
        "radius_km": 200,
        "detail": "US shale revolution epicenter; Texas/New Mexico; 6M bpd; world's top-producing basin",
    },
    {
        "name": "Vaca Muerta",
        "country": "AR",
        "lat": -38.50,
        "lng": -69.00,
        "type": "oil/gas",
        "reserves": "~16B barrels oil + 308T cf gas",
        "radius_km": 120,
        "detail": "World's 2nd-largest shale gas, 4th-largest shale oil; Patagonia",
    },
    {
        "name": "North Sea Brent Field",
        "country": "GB/NO",
        "lat": 61.04,
        "lng": 1.72,
        "type": "oil",
        "reserves": "~4B barrels (original)",
        "radius_km": 20,
        "detail": "Benchmark crude pricing (Brent crude); UK/Norway; decommissioning phase",
    },
    {
        "name": "Tengiz Field",
        "country": "KZ",
        "lat": 46.27,
        "lng": 53.38,
        "type": "oil",
        "reserves": "~25B barrels",
        "radius_km": 25,
        "detail": "Chevron-led TCO consortium; CPC pipeline to Black Sea; $48B Future Growth Project",
    },
    {
        "name": "Rumaila Field",
        "country": "IQ",
        "lat": 30.60,
        "lng": 47.38,
        "type": "oil",
        "reserves": "~17B barrels",
        "radius_km": 30,
        "detail": "Iraq's largest producing field; 1.4M bpd; BP/CNPC operators",
    },
    {
        "name": "Johan Sverdrup",
        "country": "NO",
        "lat": 58.89,
        "lng": 2.52,
        "type": "oil",
        "reserves": "~2.7B barrels",
        "radius_km": 15,
        "detail": "Norway's largest discovery in decades; powered by shore hydroelectricity; low carbon",
    },
    {
        "name": "Marcellus Shale",
        "country": "US",
        "lat": 41.0,
        "lng": -77.5,
        "type": "gas",
        "reserves": "~500T cf gas (in-place)",
        "radius_km": 200,
        "detail": "Largest US natural gas field; Pennsylvania/WV/Ohio; ~35% of US gas production",
    },
    {
        "name": "Eagle Ford Shale",
        "country": "US",
        "lat": 28.5,
        "lng": -98.5,
        "type": "oil/gas",
        "reserves": "~10B barrels oil + 100T cf gas",
        "radius_km": 150,
        "detail": "Major South Texas shale play; oil & gas condensate; ~1.2M bpd peak",
    },
    {
        "name": "Western Canadian Sedimentary Basin",
        "country": "CA",
        "lat": 54.0,
        "lng": -116.0,
        "type": "oil/gas",
        "reserves": "~170B barrels oil (incl. oil sands)",
        "radius_km": 250,
        "detail": "Includes Athabasca oil sands; world's 3rd-largest proven reserves; Alberta",
    },
    {
        "name": "Athabasca Oil Sands",
        "country": "CA",
        "lat": 57.0,
        "lng": -111.5,
        "type": "oil",
        "reserves": "~166B barrels (proven)",
        "radius_km": 180,
        "detail": "World's largest bitumen deposit; surface mining + SAGD; high carbon intensity",
    },
    {
        "name": "Groningen Gas Field",
        "country": "NL",
        "lat": 53.3,
        "lng": 6.7,
        "type": "gas",
        "reserves": "~2.7T cm (original)",
        "radius_km": 30,
        "detail": "Once Europe's largest gas field; production halted 2024 due to earthquake risk",
    },
    {
        "name": "South Pars / North Dome",
        "country": "QA/IR",
        "lat": 26.5,
        "lng": 52.0,
        "type": "gas",
        "reserves": "~1,800T cf gas",
        "radius_km": 100,
        "detail": "World's largest natural gas field; shared Qatar/Iran; North Field Expansion",
    },
    {
        "name": "Hassi Messaoud",
        "country": "DZ",
        "lat": 31.67,
        "lng": 6.07,
        "type": "oil",
        "reserves": "~10B barrels (recoverable)",
        "radius_km": 50,
        "detail": "Algeria's largest oil field; discovered 1956; Sonatrach; mature but still producing",
    },
    {
        "name": "Zakum Field",
        "country": "AE",
        "lat": 24.85,
        "lng": 53.7,
        "type": "oil",
        "reserves": "~21B barrels (Upper Zakum)",
        "radius_km": 25,
        "detail": "UAE's largest offshore field; ADNOC; Upper Zakum capacity target 1M bpd",
    },
    {
        "name": "Samotlor Field",
        "country": "RU",
        "lat": 61.18,
        "lng": 76.73,
        "type": "oil",
        "reserves": "~28B barrels (original)",
        "radius_km": 60,
        "detail": "Russia's largest oil field; West Siberia; peak 3.4M bpd in 1980; now ~300K bpd",
    },
    {
        "name": "Mangala Field",
        "country": "IN",
        "lat": 27.1,
        "lng": 71.4,
        "type": "oil",
        "reserves": "~1B barrels",
        "radius_km": 15,
        "detail": "India's largest onshore oil discovery in 25 years; Rajasthan; Cairn/Vedanta",
    },
    {
        "name": "Tupi / Lula Field",
        "country": "BR",
        "lat": -25.0,
        "lng": -43.5,
        "type": "oil",
        "reserves": "~8B barrels",
        "radius_km": 40,
        "detail": "Brazil's largest producing pre-salt field; deepwater Santos Basin; Petrobras",
    },

    # ────────────────────────────────────────────────────────────────────────
    # Rare Earth & Critical Mineral Deposits
    # ────────────────────────────────────────────────────────────────────────
    {
        "name": "Bayan Obo Mine",
        "country": "CN",
        "lat": 41.78,
        "lng": 109.97,
        "type": "rare_earth",
        "reserves": "~48M tonnes REO",
        "radius_km": 30,
        "detail": "World's largest rare earth deposit; 60% of global supply; Inner Mongolia",
    },
    {
        "name": "Mountain Pass Mine",
        "country": "US",
        "lat": 35.48,
        "lng": -115.53,
        "type": "rare_earth",
        "reserves": "~1.4M tonnes REO",
        "radius_km": 15,
        "detail": "US's only active rare earth mine; MP Materials; DOD strategic interest",
    },
    {
        "name": "Mount Weld",
        "country": "AU",
        "lat": -28.77,
        "lng": 122.02,
        "type": "rare_earth",
        "reserves": "~1.7M tonnes REO",
        "radius_km": 15,
        "detail": "World's richest known rare earth deposit by grade; Lynas Corp; processed in Malaysia",
    },
    {
        "name": "Norra Karr",
        "country": "SE",
        "lat": 58.10,
        "lng": 14.60,
        "type": "rare_earth",
        "reserves": "~0.5M tonnes REO",
        "radius_km": 10,
        "detail": "Heavy rare earth deposit; EU critical minerals; permitting challenges",
    },
    {
        "name": "Kvanefjeld (Kuannersuit)",
        "country": "GL",
        "lat": 60.98,
        "lng": -46.02,
        "type": "rare_earth",
        "reserves": "~0.7M tonnes REO",
        "radius_km": 15,
        "detail": "One of world's largest undeveloped deposits; Greenland banned uranium mining",
    },
    {
        "name": "Steenkampskraal",
        "country": "ZA",
        "lat": -31.30,
        "lng": 18.75,
        "type": "rare_earth",
        "reserves": "~0.1M tonnes REO",
        "radius_km": 10,
        "detail": "High-grade monazite deposit; thorium co-product; small-scale restart",
    },
    {
        "name": "Serra Verde",
        "country": "BR",
        "lat": -13.80,
        "lng": -48.50,
        "type": "rare_earth",
        "reserves": "~0.3M tonnes REO",
        "radius_km": 15,
        "detail": "Ionic clay deposit; low-cost extraction; Goias state; diversification from China",
    },
    {
        "name": "Kolwezi Cobalt Belt",
        "country": "CD",
        "lat": -10.72,
        "lng": 25.47,
        "type": "cobalt/copper",
        "reserves": "70% of global cobalt reserves",
        "radius_km": 80,
        "detail": "DRC copper-cobalt belt; critical for EV batteries; child labor concerns",
    },
    {
        "name": "Pilbara Lithium Province",
        "country": "AU",
        "lat": -21.83,
        "lng": 119.02,
        "type": "lithium",
        "reserves": "Major spodumene deposits",
        "radius_km": 100,
        "detail": "Greenbushes, Pilgangoora, Wodgina mines; world's #1 hard-rock lithium producer",
    },
    {
        "name": "Atacama Lithium Triangle",
        "country": "CL",
        "lat": -23.50,
        "lng": -68.20,
        "type": "lithium",
        "reserves": "~50% global lithium reserves",
        "radius_km": 150,
        "detail": "Salar de Atacama; SQM/Albemarle; Chile, Argentina, Bolivia triangle",
    },
    {
        "name": "Bushveld Complex (PGMs)",
        "country": "ZA",
        "lat": -24.90,
        "lng": 29.45,
        "type": "platinum_group",
        "reserves": "75% of global platinum reserves",
        "radius_km": 120,
        "detail": "World's largest PGM deposit; platinum, palladium, rhodium; Anglo American, Impala",
    },
    {
        "name": "Carajas Mine",
        "country": "BR",
        "lat": -6.07,
        "lng": -50.19,
        "type": "iron_ore",
        "reserves": "~18B tonnes iron ore",
        "radius_km": 60,
        "detail": "World's largest iron ore mine; Vale; extremely high grade (66% Fe); Amazon region",
    },
    {
        "name": "Olympic Dam",
        "country": "AU",
        "lat": -30.45,
        "lng": 136.89,
        "type": "uranium/copper",
        "reserves": "Largest uranium deposit + major copper/gold",
        "radius_km": 40,
        "detail": "BHP; world's largest uranium deposit; 4th-largest copper; underground mine; S. Australia",
    },
    {
        "name": "Morenci Mine",
        "country": "US",
        "lat": 33.09,
        "lng": -109.35,
        "type": "copper",
        "reserves": "Major copper porphyry deposit",
        "radius_km": 25,
        "detail": "Largest copper mine in North America; Freeport-McMoRan; Arizona open-pit",
    },
]
