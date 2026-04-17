FROM elasticsearch:8.17.0
RUN bin/elasticsearch-plugin install analysis-nori
