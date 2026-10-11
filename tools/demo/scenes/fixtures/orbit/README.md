# orbit

A small command line tool that predicts satellite passes for your location.
(A made-up project: the demo session in tools/demo/make_all.sh reads this file.)

## Usage

    orbit next --city Montreal     the next visible pass: time, direction, peak elevation
    orbit watch ISS                live positions while the satellite crosses the sky

Passes are computed locally from cached orbital data. Nothing is uploaded.
