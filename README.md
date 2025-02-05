# LibreGeoLens

A QGIS plugin for experimenting with Multimodal Large Language Models (MLLMs) to analyze remote sensing imagery.

## Demo (click on the image)

TODO.

## Features

- Chat with an MLLM about georeferenced imagery.
- Choose from different MLLM services and models.
- Work with local or remote imagery. Toggle on and off imagery layers as needed.
- To work with remote imagery, load a GeoJSON containing imagery outlines and remote paths to Cloud Optimized GeoTIFFs (COGs),
  and draw areas to select the ones to stream and display.
- The GeoJSON can be loaded from local storage or from S3. Any existing layers from having loaded a previous GeoJSON will be removed.
  A default S3 directory can be set, and the `.geojson` files in it will be displayed as a dropdown choice, sorted by most recent to oldest.
- Create new chats and keep track of existing chats.
- Draw areas to select imagery chips to chat with the MLLM about. Chips can be extracted from the raw imagery or from the screen display.
  Multiple chips can be used at the same time.
- Visualize the selected chips that will be sent to the MLLM. Double-click on them to open them with your computer's default image viewer.
  One-click on them to flash and/or zoom to their corresponding GeoJSON features (see below).
  One-clicking on the chips in the chat also does this, as well as selecting them in case we want to use them again.
  Unselecting the chips will remove their corresponding drawn areas (temporary features) if they haven't been sent yet to the MLLM.
- Log interactions as GeoJSON features. Save them to a local directory. Optionally back them up in S3.
  Keep track of them as polygons that can be selected to take you to where they were used in the chat/s.
  If more than one feature contains the selection point, a dropdown will allow you to choose which feature you want to select.

## Prerequisites

### MLLM services

Right now the plugin only supports [OpenAI](https://platform.openai.com/docs/overview) (paid)
and [Groq](https://console.groq.com/) (free), and you need an API key to use either of them. Open QGIS and go to
Settings -> Options -> System -> scroll down to Environment, toggle if needed, click on the "Use custom variables" checkbox,
and add at least one of the following environment variables:
- Variable: `OPENAI_API_KEY` - Value: your OpenAI API key.
- Variable: `GROQ_API_KEY` - Value: your Groq API key.

Make sure to restart QGIS so that these changes take effect.

### COG streaming (optional)

Since you can use the plugin with your local imagery, this is optional. 
We also provide a couple of demo images hosted in S3 so that you can try out the plugin even if you don't have any imagery.
So feel free to skip this section for now.

However, we've found it convenient to use the COG streaming functionality that QGIS provides,
and so we've added features to the plugin accordingly. We've tested this over HTTPS hosted in a public AWS S3,
as well as with a private S3 bucket, but it should work with other clouds as well, as long as you set the right environment variables.
For S3, you need to add the following environment variables (the same way we did above for the MLLM services):
- Variable: `AWS_ACCESS_KEY_ID` - Value: See [here](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html).
- Variable: `AWS_SECRET_ACCESS_KEY` - Value: idem as above.
- Variable: `AWS_REQUEST_PAYER` - Value: `requester`.

Finally, you need COGs hosted either publicly or in private cloud storage, and a GeoJSON file that the plugin can load.

<details>

<summary>Expand to see the GeoJSON format.</summary>

```json
{
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        <outline_coords_in_epsg_4326>
                    ]
                ]
            },
            "properties": {
                "remote_path": "s3://path/to/cog.tif"  (for example, could also start with "https" or other cloud)
            }
        },
        ...
    ]
}
```

</details>

Also look at [this](https://libre-geo-lens.s3.us-east-1.amazonaws.com/demo/demo_imagery.geojson) for another example.

You can use [utils/create_image_outlines_geojson.py](utils/create_image_outlines_geojson.py) 
to create a `.geojson` from your COGs in S3. Just run it like this:
```shell
python create_image_outlines_geojson.py --s3_directories s3://bucket1/path/to/dir1/ s3://bucket2/path/to/dir2/ 
```

## Quickstart

1. Make sure you have followed the instructions for the prerequisites above.
2. Search for "LibreGeoLens" in Plugins -> Manage and Install Plugins... -> All, and click Install Plugin.
3. Load a basemap layer. See [this](https://www.giscourse.com/quickmapservices-plugin-an-easy-way-to-add-basemaps-in-qgis/) for an example of one way to do it. Google Road is a nice one to start with.
4. Click on the <img src="LibreGeoLens/resources/icons/icon.png" width="20" height="20"> icon on the top right to start 
   the plugin, which will be docked to your right.
5. Click on the Load GeoJSON button, choose "Use Demo Resources" and click Ok.
6. You will see two red polygons over the US. Zoom into one of them, click on the Draw Area to Stream COGs button,
   and draw an area that intersects with one of them (click once on the map to start drawing and a second time to finish). 
   The COG will be displayed.
7. Zoom into the image and find something you want to chat with the MLLM about.
8. Click on the Draw Area to Chip Imagery button, draw the area the same way you did before, 
   and you'll see the chip above the green Send to MLLM button.
9. Type a prompt and click on the green button to start a conversation.

## More functionality

- Send Screen Chip to capture the screen display or Send Raw Chips to extract the actual pixels from the image layer.
- You can send multiple chips (or no chips).
- After clicking on the green Send button, each chip is saved as a GeoJSON feature and displayed as an orange rectangle.
- Click on the Select Area button and then click on a GeoJSON feature to see where it was used in the chat/s.
- Click on a chip in the chat to select it, and highlight (and zoom to if needed) its GeoJSON feature
- Click on a chip above the green button to highlight (and zoom to if needed) its GeoJSON feature.
- Double-click on a chip above the green button to open it with your machine's image viewer.
- You can choose between different MLLM services and models.
- You can manually load local GeoTIFFs / COGs instead of using COG streaming.
- You can stream your own data. See the COG streaming subsection above for more details. 
  GeoJSONs can be loaded locally or from S3 through the Load GeoJSON button.
- Additional optional settings  <img src="LibreGeoLens/resources/icons/settings_icon.png" width="20" height="20">:
    - `Default GeoJSON S3 Directory`: the default directory in S3 where the `.geojson` files will be searched for.
    - `S3 Logs Directory`: the directory in S3 where you want to back up your logs.
       You can leave it blank if you don't want to back them up automatically.
    - `Local Logs Directory`: the local directory where you want to save your logs. You can use the Browse button for this one.
       If you don't set it, a new directory called `LibreGeoLensLogs` will be created in your home directory and the logs will be saved here.

## Installation from source (for devs)

1. Clone this repo and follow the prerequisites section above.
2. Run `pip install -t .\LibreGeoLens\libs -r .\requirements.txt` from main directory of this repo.
3. Find your QGIS local plugins directory and symlink [LibreGeoLens](LibreGeoLens) (the inner directory).
   NOTE: If it's your first time using a QGIS plugin, you'll need to create the `plugins` directory first (see below).

In Windows, you can run Command Prompt as an Administrator and do:
```
mklink /D "C:\Users\<UserName>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\LibreGeoLens" "C:\local\path\to\LibreGeoLens\LibreGeoLens"
```
In macOS, you can do:
```
ln -s /absolute/local/path/to/LibreGeoLens/LibreGeoLens ~/Library/Application\ Support/QGIS/QGIS3/profiles/default/python/plugins/LibreGeoLens
```
In Linux, you can do:
```
ln -s /absolute/local/path/to/LibreGeoLens/LibreGeoLens ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/LibreGeoLens
```

4. Open QGIS, go to Plugins -> Manage and Install Plugins -> Settings -> Show also experimental plugins -> Installed Plugins -> LibreGeoLens.
5. Now this plugin should appear when you click on Plugins and also the icons should show up on the right.
   If the plugin still doesn't appear, close and re-open QGIS and try again.
6. In order to reload the plugin after the code in this repo is modified, you can install and use the *Plugin Reloader* plugin.
7. If you change the icons or use new resources, run `pyrcc5 -o resources.py resources.qrc`.

## Publishing

TODO
